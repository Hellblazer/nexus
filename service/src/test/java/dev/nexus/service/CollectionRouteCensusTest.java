// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.TreeSet;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a write half (nexus-p2m1x.23), the static half of the route contract. RDR line 2662: "every
 * collection-taking route accepts {@code collection_id} and a label". {@link CollectionRoutesByIdTest} runs every row of
 * {@link CollectionRouteCensus#ROUTES}; this proves the rows are every route.
 *
 * <p>Static source scan, the family of {@code KeyedRoutesCensusTest} and {@code CatalogHandlerEnvelopeConformanceGateTest}:
 * it counts, per handler file, the reads of a collection-named request field (comments blanked), pins the counts, and
 * requires every field read to be a slot of a census row or a named exclusion. A handler that starts reading a new
 * collection field, or a new site for an old one, fails here until a row or an exclusion says what the by-id contract
 * does with it. It also requires every row's path to be dispatched by the handler file the row names, and the row's
 * handler method to exist, so a row cannot outlive its route.
 *
 * <p><b>What finds a route (RDR-229 P3.5a, linda's ruling D1).</b> Not the literals of the handler files alone: a request map can
 * be read in a repository ({@code CatalogRepository.recordGcAudit} reads {@code collection}, the document routes read
 * {@code physical_collection}). Three scans, each non-vacuous:
 * <ol>
 *   <li>every read of a collection-named request field in a handler is made by a method a row, a helper or an exclusion names;</li>
 *   <li>the same in every other main source file, by the request-map reads ({@code s(map, "..."), body.get("...")});</li>
 *   <li>every call that resolves a collection through the registry, in any main file, is counted per file ({@link
 *       CollectionRouteCensus#RESOLVING}), whatever the field is called, so a repository that starts to resolve a collection from
 *       a request fails here even if it reads it under a name no scan knows. The document resolver's callers are pinned to the
 *       methods the document routes reach.</li>
 * </ol>
 * Every excluded site names its reason and a fragment of source that must still be there; none is "ambiguous, not tested".
 *
 * <p>Both directions are non-vacuous: the examined file and site counts are asserted, and a synthetic source with a new
 * field shows the scan sees it.
 *
 * <p>What {@code nonIntegerId} proves in {@link CollectionRoutesByIdTest}: on a route that requires its collection it passes
 * today because the 400 is the missing label, so it only starts to test id validation once the id is read; on a route whose
 * collection is optional it is red today (a 200), and that is the real signal.
 */
class CollectionRouteCensusTest {

    private static final Path HTTP_DIR = Path.of("src/main/java/dev/nexus/service/http");

    /** The reads of a collection-named request field: a request-reading helper called with the field name as its key. */
    private static final Pattern READ = Pattern.compile(
        "(?:queryParam|requireQueryParam|requireString|optString|optStr|optStringOrNull|optionalString|optionalNonBlank"
            + "|requireStringList|get|getOrDefault)\\(\\s*(?:\\w+\\s*,\\s*)?\"([a-z_]*collection[a-z_]*)\"");

    /**
     * Sites per handler file and field, on this tree. A count that moves means a handler gained or lost a read of a
     * collection field: add or remove the census row (or exclusion) and change the number here.
     */
    private static final Map<String, Map<String, Integer>> PINNED = Map.ofEntries(
        Map.entry("AspectHandler.java", Map.of("collection", 4)),
        Map.entry("CatalogHandler.java", Map.of("collection", 10, "prefer_collection", 1)),
        Map.entry("ChashHandler.java", Map.of("collection", 2)),
        Map.entry("ChunkKeysAdminHandler.java", Map.of("collection", 3, "collection_id", 2)),
        Map.entry("CollectionSelectorFilter.java", Map.of("physical_collection_id", 1)),
        Map.entry("PipelineHandler.java", Map.of("collection", 4)),
        Map.entry("ReembedHandler.java", Map.of("source_collection", 2, "target_collection", 2)),
        Map.entry("RemapHandler.java", Map.of("source_collection", 4, "target_collection", 2)),
        Map.entry("TaxonomyHandler.java", Map.of("collection", 24, "source_collection", 3, "cross_collection", 2,
            "source_collection_prefix", 1, "collections", 1, "collection_name", 1)),
        Map.entry("TelemetryHandler.java", Map.of("collection", 6)),
        Map.entry("VectorHandler.java", Map.of("collection", 14, "collections", 6, "quarantine_collection", 3,
            "origin_collection", 3)));

    /** {@code src} with its comments blanked. A comment marker inside a string literal is not one (the earlier regex took it for one). */
    static String blank(String src) {
        return scan(src)[0];
    }

    /**
     * {@code {code, masked}}: {@code src} with its comments blanked, and that with the content of every string and character
     * literal replaced by {@code x}. Same length as {@code src}, so a position in one is a position in the other: the masked text is
     * for finding declarations and matching braces, the code text for reading a literal.
     */
    static String[] scan(String src) {
        int n = src.length();
        char[] code = src.toCharArray();
        char[] masked = src.toCharArray();
        int i = 0;
        while (i < n) {
            char c = src.charAt(i);
            char next = i + 1 < n ? src.charAt(i + 1) : '\0';
            if (c == '/' && next == '/') {
                int j = src.indexOf('\n', i);
                j = j < 0 ? n : j;
                for (int k = i; k < j; k++) {
                    code[k] = ' ';
                    masked[k] = ' ';
                }
                i = j;
            } else if (c == '/' && next == '*') {
                int j = src.indexOf("*/", i + 2);
                j = j < 0 ? n : j + 2;
                for (int k = i; k < j; k++) {
                    if (src.charAt(k) != '\n') {
                        code[k] = ' ';
                        masked[k] = ' ';
                    }
                }
                i = j;
            } else if (src.startsWith("\"\"\"", i)) {
                int j = src.indexOf("\"\"\"", i + 3);
                j = j < 0 ? n : j + 3;
                for (int k = i + 3; k < j - 3; k++) {
                    if (src.charAt(k) != '\n') {
                        masked[k] = 'x';
                    }
                }
                i = j;
            } else if (c == '"' || c == '\'') {
                int j = i + 1;
                while (j < n && src.charAt(j) != c) {
                    j += src.charAt(j) == '\\' ? 2 : 1;
                }
                for (int k = i + 1; k < Math.min(j, n); k++) {
                    masked[k] = 'x';
                }
                i = j + 1;
            } else {
                i++;
            }
        }
        return new String[] {new String(code), new String(masked)};
    }

    /** A method declaration, in masked source: group 1 is its name. Calls and control statements do not match (no {@code ;} or brace before the open brace). */
    private static final Pattern DECL = Pattern.compile(
        "(?m)^[ \\t]+(?:@\\w+\\s+)*(?:(?:private|public|protected|static|final|synchronized)\\s+)*(?:<[^>]+>\\s+)?"
            + "[\\w.\\[\\]]+(?:<[^;{}()]*>)?\\s+(\\w+)\\s*\\([^;{}]*\\)\\s*(?:throws\\s+[\\w., ]+)?\\{");

    /** The reads of a collection-named field from a request map in a non-handler: a keyed helper or a map receiver, then the literal. */
    private static final Pattern READ_MAP = Pattern.compile(
        "(?:\\b(?:s|optString|optionalString|optStr|requireString|str)\\(\\s*\\w+\\s*,\\s*"
            + "|\\b(?:body|fields|d|audit|req|request|m|update|spec)\\.(?:get|getOrDefault|containsKey|remove)\\(\\s*)"
            + "\"([a-z_]*collection[a-z_]*)\"");

    /** A call that resolves a collection through the registry or the id helpers. */
    private static final Pattern RESOLVE_CALL = Pattern.compile(
        "\\b(?:CollectionRegistry\\s*\\.\\s*shared\\s*\\(\\s*\\)|CollectionRegistry|CollectionIds|CatalogIdStamps|DocumentCollectionRef|registry)\\s*\\.\\s*"
            + "(?:require\\w*|lookup\\w*|resolve\\w*|idOf|requireMemo|normalize\\w*|lifecycleState)\\s*\\(");

    private static final Path MAIN_DIR = Path.of("src/main/java/dev/nexus/service");

    /** One declaration of a method, and the extent of its body in the masked source. */
    private record Decl(String name, int start, int end) { }

    private static List<Decl> declarations(String masked) {
        List<Decl> out = new ArrayList<>();
        Matcher m = DECL.matcher(masked);
        while (m.find()) {
            int depth = 0;
            int j = m.end() - 1;
            for (; j < masked.length(); j++) {
                char c = masked.charAt(j);
                if (c == '{') {
                    depth++;
                } else if (c == '}' && --depth == 0) {
                    break;
                }
            }
            out.add(new Decl(m.group(1), m.start(), j));
        }
        return out;
    }

    /** The method a position is in: the last declaration that starts before it, or {@code ?}. */
    private static String enclosing(List<Decl> decls, int position) {
        String name = "?";
        for (Decl d : decls) {
            if (d.start() < position) {
                name = d.name();
            }
        }
        return name;
    }

    /** Every main source file, by its path under {@link #MAIN_DIR}, with its code and masked text. */
    private static Map<String, String[]> mainSources() throws Exception {
        Map<String, String[]> out = new TreeMap<>();
        try (Stream<Path> walk = Files.walk(MAIN_DIR)) {
            for (Path p : (Iterable<Path>) walk.filter(f -> f.toString().endsWith(".java"))::iterator) {
                out.put(MAIN_DIR.relativize(p).toString().replace('\\', '/'), scan(Files.readString(p)));
            }
        }
        return out;
    }

    static Map<String, Integer> fieldReads(String src) {
        Map<String, Integer> out = new TreeMap<>();
        Matcher m = READ.matcher(blank(src));
        while (m.find()) {
            out.merge(m.group(1), 1, Integer::sum);
        }
        return out;
    }

    @Test
    void everyCollectionFieldAHandlerReadsIsARowOrANamedExclusion() throws Exception {
        assertThat(HTTP_DIR).as("the census runs from the service module directory").exists();
        Map<String, Map<String, Integer>> found = new TreeMap<>();
        int examined = 0;
        try (Stream<Path> files = Files.list(HTTP_DIR)) {
            for (Path p : (Iterable<Path>) files.filter(f -> f.toString().endsWith(".java"))::iterator) {
                examined++;
                Map<String, Integer> reads = fieldReads(Files.readString(p));
                if (!reads.isEmpty()) {
                    found.put(p.getFileName().toString(), reads);
                }
            }
        }
        assertThat(examined).as("non-vacuity: the handler directory was read").isGreaterThan(30);
        assertThat(found.keySet()).as("non-vacuity: the handlers that read a collection field").hasSizeGreaterThanOrEqualTo(10);
        assertThat(found.values().stream().mapToInt(m -> m.values().stream().mapToInt(Integer::intValue).sum()).sum())
            .as("non-vacuity: the read sites examined").isGreaterThanOrEqualTo(90);

        assertThat(found).as("a handler read site was added or removed: classify it in CollectionRouteCensus, then change PINNED")
            .isEqualTo(PINNED);

        // Every site is made by a method the census names: a row's handler method, a helper a row calls, or an exclusion.
        Map<String, Set<String>> named = new TreeMap<>();
        for (var route : CollectionRouteCensus.allRoutes()) {
            named.computeIfAbsent(route.handler(), k -> new TreeSet<>()).add(route.readsId());
        }
        for (var rename : CollectionRouteCensus.STORE_RENAMES) {
            named.computeIfAbsent(rename.handler(), k -> new TreeSet<>()).add(rename.readsId());
        }
        for (var helper : CollectionRouteCensus.HELPERS) {
            named.computeIfAbsent(helper.file(), k -> new TreeSet<>()).add(helper.method());
        }
        for (var ex : CollectionRouteCensus.EXCLUDED) {
            named.computeIfAbsent(baseName(ex.file()), k -> new TreeSet<>()).add(ex.method());
        }
        Map<String, Set<String>> unaccounted = new TreeMap<>();
        Set<String> sites = new TreeSet<>();
        for (var e : mainSources().entrySet()) {
            if (!e.getKey().startsWith("http/")) {
                continue;
            }
            String file = baseName(e.getKey());
            List<Decl> decls = declarations(e.getValue()[1]);
            Matcher m = READ.matcher(e.getValue()[0]);
            while (m.find()) {
                String method = enclosing(decls, m.start());
                sites.add(file + "#" + method + "#" + m.group(1));
                if (!named.getOrDefault(file, Set.of()).contains(method)) {
                    unaccounted.computeIfAbsent(file, k -> new TreeSet<>()).add(method + " reads '" + m.group(1) + "'");
                }
            }
        }
        assertThat(sites).as("non-vacuity: the (file, method, field) sites examined in the handlers").hasSizeGreaterThanOrEqualTo(70);
        assertThat(unaccounted).as("a handler method reads a collection field that no row, helper or exclusion of "
            + "CollectionRouteCensus names: add its route as a row (and to CollectionSelectors), or EXCLUDED with the reason").isEmpty();
    }

    private static String baseName(String path) {
        return path.substring(path.lastIndexOf('/') + 1);
    }

    /**
     * The same accounting for every main source file that is not a handler: a repository or helper that reads a collection-named
     * field from a request map. Found here, not in the handlers: {@code CatalogRepository.recordGcAudit} (the audit row's
     * {@code collection}, SB3), the document routes' {@code physical_collection}, the aspect repository's upserts. A method is
     * accounted for by a row's {@code repo} entry or an exclusion; an exclusion's site must exist, so none outlives its code.
     */
    @Test
    void everyCollectionFieldARepositoryReadsFromARequestMapIsARowsOrAnExclusionsMethod() throws Exception {
        Map<String, Set<String>> named = new TreeMap<>();
        for (var route : CollectionRouteCensus.allRoutes()) {
            if (!route.repo().isEmpty()) {
                String[] parts = route.repo().split("#");
                named.computeIfAbsent(parts[0], k -> new TreeSet<>()).addAll(List.of(parts[1].split(",")));
            }
        }
        Set<String> excluded = new TreeSet<>();
        for (var ex : CollectionRouteCensus.EXCLUDED) {
            // the handlers and http/CollectionSelectorFilter are the handler census's; this one reads outside http/
            if (!ex.file().endsWith("Handler.java") && !ex.file().equals("CollectionSelectorFilter.java")) {
                named.computeIfAbsent(ex.file(), k -> new TreeSet<>()).add(ex.method());
                excluded.add(ex.file() + "#" + ex.method() + "#" + ex.field());
            }
        }
        Map<String, Set<String>> unaccounted = new TreeMap<>();
        Set<String> sites = new TreeSet<>();
        Map<String, Set<String>> declared = new TreeMap<>();
        int files = 0;
        for (var e : mainSources().entrySet()) {
            if (e.getKey().startsWith("http/")) {
                continue;
            }
            files++;
            String file = baseName(e.getKey());
            List<Decl> decls = declarations(e.getValue()[1]);
            for (Decl d : decls) {
                declared.computeIfAbsent(file, k -> new TreeSet<>()).add(d.name());
            }
            Matcher m = READ_MAP.matcher(e.getValue()[0]);
            while (m.find()) {
                String method = enclosing(decls, m.start());
                sites.add(file + "#" + method + "#" + m.group(1));
                if (!named.getOrDefault(file, Set.of()).contains(method)) {
                    unaccounted.computeIfAbsent(file, k -> new TreeSet<>()).add(method + " reads '" + m.group(1) + "'");
                }
            }
        }
        assertThat(files).as("non-vacuity: the non-handler sources examined").isGreaterThan(250);
        assertThat(sites).as("non-vacuity: the request-map read sites examined outside the handlers").hasSizeGreaterThanOrEqualTo(12);
        assertThat(sites).as("the gc_audit record route's read is found, in the repository (SB3)")
            .contains("CatalogRepository.java#recordGcAudit#collection");
        assertThat(sites).as("the document routes' reads are found, in the repository")
            .contains("CatalogRepository.java#upsertDocument#physical_collection",
                "CatalogRepository.java#registerDocumentWithOutcome#physical_collection",
                "CatalogRepository.java#resolveOrRegister#physical_collection");
        assertThat(unaccounted).as("a repository method reads a collection field from a request map that no row's repo entry "
            + "or exclusion of CollectionRouteCensus names").isEmpty();
        assertThat(sites).as("every exclusion names a method that still reads its field").containsAll(excluded);
        for (var e : named.entrySet()) {
            assertThat(declared.getOrDefault(e.getKey(), Set.of())).as("%s declares every method the census names in it", e.getKey())
                .containsAll(e.getValue());
        }
    }

    /**
     * Every excluded site keeps its reason true: the evidence fragment is still in the source it names, and the exclusion's own
     * handler site exists (a handler exclusion is one of the sites the handler scan found).
     */
    @Test
    void everyExclusionsEvidenceIsStillInTheSource() throws Exception {
        Map<String, String[]> sources = mainSources();
        Set<String> handlerSites = new TreeSet<>();
        for (var e : sources.entrySet()) {
            if (e.getKey().startsWith("http/")) {
                List<Decl> decls = declarations(e.getValue()[1]);
                Matcher m = READ.matcher(e.getValue()[0]);
                while (m.find()) {
                    handlerSites.add(baseName(e.getKey()) + "#" + enclosing(decls, m.start()) + "#" + m.group(1));
                }
            }
        }
        assertThat(CollectionRouteCensus.EXCLUDED).as("non-vacuity: the exclusions").hasSizeGreaterThanOrEqualTo(10);
        for (var ex : CollectionRouteCensus.EXCLUDED) {
            String key = sources.keySet().stream().filter(k -> baseName(k).equals(ex.evidenceFile())).findFirst().orElseThrow();
            assertThat(sources.get(key)[0]).as("%s#%s: its reason rests on '%s'", ex.file(), ex.method(), ex.evidence())
                .contains(ex.evidence());
            assertThat(ex.why()).as("%s#%s names its reason", ex.file(), ex.method()).hasSizeGreaterThan(40)
                .doesNotContainIgnoringCase("ambiguous").doesNotContainIgnoringCase("not tested");
            if (ex.file().endsWith("Handler.java") || ex.file().equals("CollectionSelectorFilter.java")) {
                assertThat(handlerSites).as("%s#%s still reads '%s'", ex.file(), ex.method(), ex.field())
                    .contains(ex.file() + "#" + ex.method() + "#" + ex.field());
            }
        }
    }

    /** A helper's rows call it: a row that names a helper in {@code usedBy} does, so the helper's sites are theirs. */
    @Test
    void everyHelperIsCalledByTheRowsThatClaimIt() throws Exception {
        Map<String, String[]> sources = mainSources();
        for (var helper : CollectionRouteCensus.HELPERS) {
            String key = "http/" + helper.file();
            List<Decl> decls = declarations(sources.get(key)[1]);
            assertThat(decls.stream().map(Decl::name)).as("%s declares %s", helper.file(), helper.method()).contains(helper.method());
            Pattern call = Pattern.compile("\\b" + Pattern.quote(helper.method()) + "\\s*\\(");
            for (String user : helper.usedBy()) {
                boolean calls = decls.stream().filter(d -> d.name().equals(user))
                    .anyMatch(d -> call.matcher(sources.get(key)[1].substring(d.start(), d.end() + 1)).find());
                assertThat(calls).as("%s#%s calls %s", helper.file(), user, helper.method()).isTrue();
            }
        }
    }

    /**
     * The scan by resolver call: every main file that resolves a collection through the registry, with its number of call sites,
     * is {@link CollectionRouteCensus#RESOLVING}, whatever field it reads. A repository that starts resolving a collection from a
     * request, under any name, changes a count here.
     */
    @Test
    void everyCallThatResolvesACollectionIsCountedPerFile() throws Exception {
        Map<String, Integer> found = new TreeMap<>();
        int files = 0;
        for (var e : mainSources().entrySet()) {
            files++;
            Matcher m = RESOLVE_CALL.matcher(e.getValue()[0]);
            int n = 0;
            while (m.find()) {
                n++;
            }
            if (n > 0) {
                found.put(e.getKey(), n);
            }
        }
        Map<String, Integer> census = new TreeMap<>();
        CollectionRouteCensus.RESOLVING.forEach((k, v) -> census.put(k, v.sites()));
        assertThat(files).as("non-vacuity: the main sources examined").isGreaterThan(300);
        assertThat(found.values().stream().mapToInt(Integer::intValue).sum()).as("non-vacuity: the resolver call sites examined")
            .isGreaterThanOrEqualTo(100);
        assertThat(found).as("a file gained or lost a call that resolves a collection: say which route it serves in "
            + "CollectionRouteCensus.RESOLVING, add the route if it is new, and change the count").isEqualTo(census);
    }

    /**
     * The document routes' resolver: the methods of {@code CatalogRepository} that call {@code DocumentCollectionRef.normalize} are
     * exactly the ones the document rows name as reached, each row's handler calls the first of them, and the table has no entry for a
     * document route (the boundary would turn the id back into a label before the write).
     */
    @Test
    void theDocumentRoutesAreTheMethodsThatCallTheDocumentResolver() throws Exception {
        Map<String, String[]> sources = mainSources();
        String[] repo = sources.get("db/CatalogRepository.java");
        List<Decl> decls = declarations(repo[1]);
        Set<String> callers = new TreeSet<>();
        Matcher m = Pattern.compile("\\bDocumentCollectionRef\\s*\\.\\s*normalize\\w*\\s*\\(").matcher(repo[0]);
        while (m.find()) {
            callers.add(enclosing(decls, m.start()));
        }
        assertThat(callers).as("the repository methods that resolve a document request's collection id").containsExactlyInAnyOrder(
            "upsertDocument", "doImportDocument", "importDocumentsBatch", "registerDocumentWithOutcome",
            "registerDocumentManyWithOutcome", "updateDocument", "updateDocumentsMany", "resolveOrRegister");

        Set<String> reached = new TreeSet<>();
        assertThat(CollectionRouteCensus.DOCUMENT_ROUTES).as("non-vacuity: the document routes").hasSize(7);
        for (var route : CollectionRouteCensus.DOCUMENT_ROUTES) {
            assertThat(route.resolver()).isEqualTo(CollectionRouteCensus.Resolver.WRITE);
            List<String> methods = List.of(route.repo().split("#")[1].split(","));
            reached.addAll(methods);
            String handler = blank(sources.get("http/" + route.handler())[0]);
            List<Decl> hd = declarations(sources.get("http/" + route.handler())[1]);
            boolean calls = hd.stream().filter(d -> d.name().equals(route.readsId()))
                .anyMatch(d -> handler.substring(d.start(), d.end() + 1).contains("repo." + methods.get(0) + "("));
            assertThat(calls).as("%s: %s calls repo.%s", route.label(), route.readsId(), methods.get(0)).isTrue();
        }
        assertThat(reached).as("every method that resolves the id is reached by a document row").containsAll(callers);
        assertThat(CollectionRouteCensusView.lines()).as("no document route is a row of the route table")
            .noneMatch(l -> l.contains("physical_collection_id"));
    }

    /** What the census examined, so a pass over nothing cannot look like a pass. */
    @Test
    void theCensusSaysWhatItExamined() {
        int boundary = (int) CollectionRouteCensus.ROUTES.stream()
            .filter(r -> r.resolver() == CollectionRouteCensus.Resolver.BOUNDARY).count();
        assertThat(boundary).as("rows the route boundary resolves").isEqualTo(CollectionRouteCensus.ROUTES.size())
            .isGreaterThanOrEqualTo(100);
        assertThat(CollectionRouteCensus.allRoutes()).as("every census route").hasSize(boundary + 7);
        assertThat(CollectionRouteCensus.EXCLUDED).as("excluded sites, each with a reason and evidence").hasSizeGreaterThanOrEqualTo(14);
    }

    /**
     * The embed and re-embed routes answer 503 before they read their collection on the standard engine (no embedder, not
     * local), so a selector check can only reach them on the LOCAL_EMBED variant. Exactly these three rows use it; every other
     * row runs on the standard engine, whose mode is unchanged.
     */
    @Test
    void exactlyTheEmbedAndReembedRowsRunOnTheLocalEmbedEngine() {
        Set<String> local = new TreeSet<>();
        for (var route : CollectionRouteCensus.allRoutes()) {
            if (route.engine() == CollectionRouteCensus.Engine.LOCAL_EMBED) {
                local.add(route.label());
            }
        }
        assertThat(local).containsExactly("POST /v1/vectors/embed", "POST /v1/vectors/reembed/jobs",
            "POST /v1/vectors/reembed/plan");
    }

    /**
     * A selector can sit inside each element of a request array (the centroid upsert reads "collection" from every element of
     * "records", a remap batch "target_collection" from every entry, the document batches "physical_collection" from every doc). The
     * census names that array, a row has at most one array, the id twin keeps the convention's spelling, and the handler source
     * really names the array. These are the nested rows.
     */
    @Test
    void theNestedSelectorRowsNameTheirArrayAndTheirHandlersReadIt() throws Exception {
        Set<String> nested = new TreeSet<>();
        for (var route : CollectionRouteCensus.allRoutes()) {
            Set<String> arrays = new TreeSet<>();
            for (var slot : route.slots()) {
                if (slot.nested()) {
                    nested.add(route.label());
                    arrays.add(slot.nestedIn());
                    assertThat(slot.idField()).isEqualTo(expectedIdField(slot));
                    String src = blank(Files.readString(HTTP_DIR.resolve(route.handler())));
                    assertThat(src).as("%s: %s names the array '%s'", route.label(), route.handler(), slot.nestedIn())
                        .contains("\"" + slot.nestedIn() + "\"");
                }
            }
            assertThat(arrays).as("%s: one array of selectors at most", route.label()).hasSizeLessThanOrEqualTo(1);
        }
        assertThat(nested).containsExactly("POST /v1/catalog/doc/register_many", "POST /v1/catalog/update_many",
            "POST /v1/remap/record_batch", "POST /v1/taxonomy/centroids/upsert");
    }

    @Test
    void everyRowIsDispatchedByItsHandlerAndItsMethodExists() throws Exception {
        assertThat(CollectionRouteCensus.allRoutes()).as("non-vacuity").hasSizeGreaterThanOrEqualTo(80);
        Set<String> seen = new HashSet<>();
        for (var route : CollectionRouteCensus.allRoutes()) {
            assertThat(seen.add(route.label())).as("%s is listed twice", route.label()).isTrue();
            String prefix = CollectionRouteCensus.PREFIX.get(route.handler());
            assertThat(prefix).as("no path prefix for %s", route.handler()).isNotNull();
            assertThat(route.path()).startsWith(prefix);
            String suffix = route.path().substring(prefix.length());
            String src = blank(Files.readString(HTTP_DIR.resolve(route.handler())));
            assertThat(src).as("%s: %s does not dispatch %s", route.label(), route.handler(), suffix)
                .contains("\"" + suffix + "\"");
            assertThat(Pattern.compile("\\b" + Pattern.quote(route.readsId()) + "\\s*\\(").matcher(src).find())
                .as("%s: %s has no method %s", route.label(), route.handler(), route.readsId()).isTrue();
        }
    }

    // ---- linda's ruling 1 (2026-11-22): one id spelling ----------------------------------------------------------------

    /** The id twin the convention gives a collection field: {@code collection_id}, {@code collection_ids}, or {@code <x>_id}. */
    static String expectedIdField(CollectionRouteCensus.Slot slot) {
        if (slot.list()) {
            return "collections".equals(slot.labelField()) ? "collection_ids" : slot.labelField() + "_ids";
        }
        return switch (slot.labelField()) {
            case "collection", "name", "old_name", "collection_name" -> "collection_id";
            default -> slot.labelField() + "_id";
        };
    }

    /** A token literal whose shape says it names a collection id: {@code collectionId}, {@code coll_id}, {@code id_of_collection}, {@code cid}. */
    private static final Pattern TOKEN_LITERAL = Pattern.compile("\"([A-Za-z][A-Za-z_\\-]*)\"");

    static boolean looksLikeACollectionId(String literal) {
        String n = literal.toLowerCase(java.util.Locale.ROOT).replace("_", "").replace("-", "");
        return n.contains("collectionid") || n.contains("collid") || n.contains("idcollection")
            || n.contains("idofcollection") || n.equals("cid");
    }

    /** The collection-id-shaped token literals in {@code src} (comments blanked) that are not in {@code allowed}. */
    static Set<String> idSpellingsOutside(String src, Set<String> allowed) {
        Set<String> out = new TreeSet<>();
        Matcher m = TOKEN_LITERAL.matcher(blank(src));
        while (m.find()) {
            String lit = m.group(1);
            if (looksLikeACollectionId(lit) && !allowed.contains(lit)) {
                out.add(lit);
            }
        }
        return out;
    }

    /**
     * The id is spelled one way: {@code collection_id} for a single-valued {@code collection}/{@code name}/{@code old_name},
     * {@code collection_ids} for the list {@code collections}, {@code <x>_id} for every other field {@code x}. Two checks.
     * The census rows follow the convention, so a row cannot quietly pick another spelling; and no handler source mentions a
     * collection-id-shaped name that is neither a row's id field nor one of the named response and reason names, so a
     * route that reads {@code collectionId} or {@code coll_id} beside the convention fails here.
     */
    @Test
    void noRouteTakesACollectionIdUnderAnotherSpelling() throws Exception {
        for (var route : CollectionRouteCensus.allRoutes()) {
            for (var slot : route.slots()) {
                assertThat(slot.idField()).as("%s: the id twin of '%s'", route.label(), slot.labelField())
                    .isEqualTo(expectedIdField(slot));
            }
        }
        Set<String> allowed = new HashSet<>(CollectionRouteCensus.ID_NAMES_OTHER_THAN_SLOTS);
        for (var route : CollectionRouteCensus.allRoutes()) {
            for (var slot : route.slots()) {
                allowed.add(slot.idField());
            }
        }
        int files = 0;
        int collectionLiterals = 0;
        Map<String, Set<String>> offenders = new TreeMap<>();
        try (Stream<Path> list = Files.list(HTTP_DIR)) {
            for (Path p : (Iterable<Path>) list.filter(f -> f.toString().endsWith(".java"))::iterator) {
                files++;
                String src = Files.readString(p);
                Matcher m = TOKEN_LITERAL.matcher(blank(src));
                while (m.find()) {
                    if (m.group(1).toLowerCase(java.util.Locale.ROOT).contains("collection")) {
                        collectionLiterals++;
                    }
                }
                Set<String> bad = idSpellingsOutside(src, allowed);
                if (!bad.isEmpty()) {
                    offenders.put(p.getFileName().toString(), bad);
                }
            }
        }
        assertThat(files).as("non-vacuity: the handler directory was read").isGreaterThan(30);
        assertThat(collectionLiterals).as("non-vacuity: the collection-named literals examined").isGreaterThanOrEqualTo(100);
        assertThat(offenders).as("a handler names a collection id under a spelling outside the convention (collection_id, "
            + "collection_ids, <x>_id of a census slot): use the convention or add the row").isEmpty();
    }

    @Test
    void theSpellingScanSeesASecondSpelling() {
        Set<String> allowed = Set.of("collection_id", "collection_ids", "source_collection_id");
        String src = """
            final class H {
                void a(Map<String, Object> body) {
                    Object a = body.get("collection_id");          // the convention
                    Object b = body.get("source_collection_id");   // a census slot
                    Object c = body.get("collectionId");           // camel case
                    Object d = body.get("coll_id");
                    Object e = body.get("id_of_collection");
                    Object f = body.get("collection-id");
                    Object g = body.get("cid");
                    Object h = body.get("topic_id");               // an id, not a collection's
                    Object i = body.get("x");                      // body.get("in_a_comment_collection_id")
                }
            }
            """;
        assertThat(idSpellingsOutside(src, allowed))
            .containsExactlyInAnyOrder("collectionId", "coll_id", "id_of_collection", "collection-id", "cid");
    }

    // ---- linda's ruling 3 (2026-11-22): every rename entry point has the refusal --------------------------------------

    private static final Pattern RENAME_COLLECTION_LITERAL = Pattern.compile("\"((?:/[a-z_]+)*/rename_collection)\"");

    /**
     * The rename refusal lives in the shared rename primitive, so each route that rewrites a collection name gets it. The
     * catalog rename is a {@link CollectionRouteCensus.Tier#RENAME} row; the per-store routes are
     * {@link CollectionRouteCensus#STORE_RENAMES}. This finds every {@code rename_collection} route the handlers dispatch and
     * requires the list to be exactly the census, so a sixth route cannot arrive without its refusal being tested in
     * {@link CollectionRenameRefusalTest}.
     */
    @Test
    void everyRenameCollectionRouteTheHandlersDispatchIsInTheRefusalCensus() throws Exception {
        Set<String> found = new TreeSet<>();
        int files = 0;
        try (Stream<Path> list = Files.list(HTTP_DIR)) {
            for (Path p : (Iterable<Path>) list.filter(f -> f.toString().endsWith(".java"))::iterator) {
                files++;
                String file = p.getFileName().toString();
                Matcher m = RENAME_COLLECTION_LITERAL.matcher(blank(Files.readString(p)));
                while (m.find()) {
                    String prefix = CollectionRouteCensus.PREFIX.get(file);
                    found.add(prefix == null ? "?" + file + m.group(1) : prefix + m.group(1));
                }
            }
        }
        assertThat(files).as("non-vacuity: the handler directory was read").isGreaterThan(30);
        assertThat(found).as("non-vacuity: the rename_collection routes found").hasSizeGreaterThanOrEqualTo(5);
        Set<String> census = new TreeSet<>();
        for (var r : CollectionRouteCensus.STORE_RENAMES) {
            census.add(r.path());
        }
        assertThat(found).as("a rename_collection route was added or removed: add or remove its STORE_RENAMES row, and its "
            + "refusal test in CollectionRenameRefusalTest").isEqualTo(census);

        for (var r : CollectionRouteCensus.STORE_RENAMES) {
            String prefix = CollectionRouteCensus.PREFIX.get(r.handler());
            assertThat(r.path()).startsWith(prefix);
            String src = blank(Files.readString(HTTP_DIR.resolve(r.handler())));
            assertThat(Pattern.compile("\\b" + Pattern.quote(r.readsId()) + "\\s*\\(").matcher(src).find())
                .as("%s: %s has no method %s", r.path(), r.handler(), r.readsId()).isTrue();
        }
    }

    @Test
    void theScanSeesAFieldItWasNotToldAbout() {
        String src = """
            final class H {
                void a(Map<String, Object> body) {
                    String c = requireString(body, "brand_new_collection");   // requireString(body, "in_a_comment_collection")
                    String d = queryParam(ex, "collection");
                }
            }
            """;
        assertThat(fieldReads(src)).containsEntry("brand_new_collection", 1).containsEntry("collection", 1)
            .doesNotContainKey("in_a_comment_collection");
    }
}
