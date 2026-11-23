// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
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
 * RDR-229 P3.5a (nexus-p2m1x.23), review of 55a26acc04 F4: {@link CollectionSelectors#WRITES} was a hand-kept list, so a route that
 * writes under a collection and was not on it (set_aspects_enabled, the gc routes) escaped the consumption census. This test reads the
 * routes from the ROUTER, not from a list: every {@code createContext} of the engine's sources gives a prefix and a handler class; every
 * {@code case "/x" ->} (or {@code op.equals("/x")}) of that handler is a route it dispatches. Against that universe:
 * <ol>
 *   <li>every route of {@link CollectionSelectors#ROUTES} (what the route boundary acts on) is a route the router dispatches;</li>
 *   <li>every POST and DELETE route of the table (a body route, or a query route whose handler method names POST or DELETE) is in
 *       exactly one of {@link CollectionSelectors#WRITES}, {@link CollectionSelectors#READS} and {@link CollectionSelectors#EXEMPT}
 *       (which names its reason), and none of the three holds a route that is not one;</li>
 *   <li>a dispatched route that is not in the table names no collection (the census, {@code CollectionRouteCensusTest}, holds the table
 *       equal to every collection field a handler reads), so it has nothing for a request by id to guard. They are counted, not listed.</li>
 * </ol>
 * The counts are printed and floored, so a scan that found nothing is a failure, not a pass.
 */
class CollectionRouteClassificationTest {

    private static final Path MAIN = Path.of("src/main/java/dev/nexus/service");

    private static final Pattern CREATE_CONTEXT = Pattern.compile(
        "createContext\\(\\s*(\"[^\"]+\"|[\\w.]+\\.PATH)\\s*,\\s*(?:new\\s+(?:[a-z0-9_]+\\.)*([A-Z][A-Za-z0-9_]*)|([\\w.]+))");
    private static final Pattern CASE_ROUTE = Pattern.compile("case\\s+\"(/[^\"]*)\"\\s*->\\s*([A-Za-z0-9_.]+)\\s*\\(");
    private static final Pattern EQUALS_ROUTE = Pattern.compile("op\\.equals\\(\"(/[^\"]+)\"\\)");
    private static final Pattern PATH_CONSTANT = Pattern.compile("\\bPATH\\s*=\\s*\"([^\"]+)\"");

    // ---- source access ------------------------------------------------------------------------------------------------------------

    /** {@code src} with its comments blanked (a string literal's content is kept; a comment marker inside one is not a comment). */
    static String blank(String src) {
        StringBuilder out = new StringBuilder(src.length());
        int n = src.length();
        int i = 0;
        while (i < n) {
            char c = src.charAt(i);
            char next = i + 1 < n ? src.charAt(i + 1) : '\0';
            if (c == '/' && next == '/') {
                while (i < n && src.charAt(i) != '\n') {
                    out.append(' ');
                    i++;
                }
            } else if (c == '/' && next == '*') {
                int end = src.indexOf("*/", i + 2);
                end = end < 0 ? n : end + 2;
                for (; i < end; i++) {
                    out.append(src.charAt(i) == '\n' ? '\n' : ' ');
                }
            } else if (src.startsWith("\"\"\"", i)) {
                int end = src.indexOf("\"\"\"", i + 3);
                end = end < 0 ? n : end + 3;
                out.append(src, i, end);
                i = end;
            } else if (c == '"' || c == '\'') {
                int j = i + 1;
                while (j < n && src.charAt(j) != c) {
                    j += src.charAt(j) == '\\' ? 2 : 1;
                }
                j = Math.min(n, j + 1);
                out.append(src, i, j);
                i = j;
            } else {
                out.append(c);
                i++;
            }
        }
        return out.toString();
    }

    private static Map<String, String> mainSources() throws IOException {
        Map<String, String> sources = new TreeMap<>();
        try (Stream<Path> files = Files.walk(MAIN)) {
            for (Path p : (Iterable<Path>) files.filter(f -> f.toString().endsWith(".java"))::iterator) {
                sources.put(MAIN.relativize(p).toString(), blank(Files.readString(p)));
            }
        }
        return sources;
    }

    private static String sourceOf(Map<String, String> sources, String simpleName) {
        String found = null;
        for (Map.Entry<String, String> e : sources.entrySet()) {
            if (e.getKey().endsWith("/" + simpleName + ".java") || e.getKey().equals(simpleName + ".java")) {
                assertThat(found).as("two files named %s", simpleName).isNull();
                found = e.getValue();
            }
        }
        assertThat(found).as("the source of handler %s", simpleName).isNotNull();
        return found;
    }

    // ---- the router --------------------------------------------------------------------------------------------------------------

    /** One dispatched route: its full path, the handler class, and (for a {@code case} route) the method the case calls. */
    record Dispatched(String path, String handler, String method) {
    }

    /** The routes the engine's contexts dispatch, read from the sources. {@code unresolved} counts contexts whose prefix is computed. */
    record Router(List<Dispatched> routes, int contexts, int unresolved) {
    }

    static Router router(Map<String, String> sources) {
        List<Dispatched> routes = new ArrayList<>();
        Set<String> seenContexts = new TreeSet<>();
        int unresolved = 0;
        for (Map.Entry<String, String> file : sources.entrySet()) {
            Matcher m = CREATE_CONTEXT.matcher(file.getValue());
            while (m.find()) {
                String prefix = resolvePrefix(sources, m.group(1));
                String handler = m.group(2) != null ? m.group(2) : handlerOfVariable(file.getValue(), m.group(3));
                if (prefix == null || handler == null || !seenContexts.add(prefix + " " + handler)) {
                    if (prefix == null || handler == null) {
                        unresolved++;
                    }
                    continue;
                }
                String src;
                try {
                    src = sourceOf(sources, handler);
                } catch (AssertionError notHere) {
                    continue;   // a handler that is not a source file of the engine (a lambda, a library class)
                }
                Matcher cases = CASE_ROUTE.matcher(src);
                while (cases.find()) {
                    routes.add(new Dispatched(prefix + cases.group(1), handler, cases.group(2)));
                }
                Matcher equals = EQUALS_ROUTE.matcher(src);
                while (equals.find()) {
                    routes.add(new Dispatched(prefix + equals.group(1), handler, null));
                }
            }
        }
        return new Router(routes, seenContexts.size(), unresolved);
    }

    private static String resolvePrefix(Map<String, String> sources, String expr) {
        if (expr.startsWith("\"")) {
            return expr.substring(1, expr.length() - 1);
        }
        String owner = expr.substring(0, expr.length() - ".PATH".length());
        owner = owner.substring(owner.lastIndexOf('.') + 1);
        try {
            Matcher m = PATH_CONSTANT.matcher(sourceOf(sources, owner));
            return m.find() ? m.group(1) : null;
        } catch (AssertionError none) {
            return null;
        }
    }

    /** {@code var x = new H(} or {@code this.x = new H(} in the same file, for a context whose handler is a variable. */
    private static String handlerOfVariable(String src, String variable) {
        if (variable == null) {
            return null;
        }
        String name = variable.startsWith("this.") ? variable.substring("this.".length()) : variable;
        Matcher m = Pattern.compile("(?:var\\s+|this\\.|\\b[A-Z][A-Za-z0-9_<>]*\\s+)" + Pattern.quote(name) + "\\s*=\\s*new\\s+(?:[a-z0-9_]+\\.)*([A-Z][A-Za-z0-9_]*)")
            .matcher(src);
        return m.find() ? m.group(1) : null;
    }

    /** The body of {@code method} in {@code src} (brace matched), or null when the handler has no such method (the case delegates). */
    static String body(String src, String method) {
        if (method == null || method.contains(".")) {
            return null;
        }
        Matcher decl = Pattern.compile("(?m)^[ \\t]+(?:private|public|protected|static|final)[\\w ]*?[\\w<>\\[\\],.?]+ "
            + Pattern.quote(method) + "\\s*\\([^)]*\\)[^{;]*\\{").matcher(src);
        if (!decl.find()) {
            return null;
        }
        int depth = 1;
        int i = decl.end();
        while (i < src.length() && depth > 0) {
            char c = src.charAt(i);
            if (c == '"') {
                i++;
                while (i < src.length() && src.charAt(i) != '"') {
                    i += src.charAt(i) == '\\' ? 2 : 1;
                }
            } else if (c == '{') {
                depth++;
            } else if (c == '}') {
                depth--;
            }
            i++;
        }
        return src.substring(decl.end(), Math.min(i, src.length()));
    }

    // ---- the test ----------------------------------------------------------------------------------------------------------------

    @Test
    void everyPostAndDeleteRouteOfTheTableIsAWriteARead_orExemptWithItsReason() throws IOException {
        Map<String, String> sources = mainSources();
        Router router = router(sources);
        Map<String, Dispatched> dispatched = new LinkedHashMap<>();
        router.routes().forEach(r -> dispatched.putIfAbsent(r.path(), r));

        assertThat(router.contexts()).as("non-vacuity: contexts read from the sources").isGreaterThanOrEqualTo(25);
        assertThat(dispatched).as("non-vacuity: routes the router dispatches").hasSizeGreaterThanOrEqualTo(300);

        // 1. what the boundary acts on is what the router dispatches
        Set<String> missing = new TreeSet<>(CollectionSelectors.ROUTES.keySet());
        missing.removeAll(dispatched.keySet());
        assertThat(missing).as("table routes no handler dispatches").isEmpty();

        // 2. the POST and DELETE routes of the table: a body route, or a query route whose handler method names POST or DELETE
        Set<String> postOrDelete = new TreeSet<>();
        List<String> queryRoutesThatWrite = new ArrayList<>();
        int queryBodiesRead = 0;
        for (Map.Entry<String, CollectionSelectors.Route> e : CollectionSelectors.ROUTES.entrySet()) {
            if (e.getValue().where() != CollectionSelectors.Where.QUERY) {
                postOrDelete.add(e.getKey());
                continue;
            }
            Dispatched d = dispatched.get(e.getKey());
            String body = body(sourceOf(sources, d.handler()), d.method());
            if (body == null) {
                continue;   // the case delegates to another class (a read route of the table; the table says it is a query route)
            }
            queryBodiesRead++;
            if (body.contains("\"POST\"") || body.contains("\"DELETE\"")) {
                queryRoutesThatWrite.add(e.getKey());
            }
        }
        assertThat(queryRoutesThatWrite).as("a QUERY row of the table whose handler accepts POST or DELETE: make it a body row")
            .isEmpty();
        assertThat(queryBodiesRead).as("non-vacuity: query routes whose handler method was read").isGreaterThanOrEqualTo(20);

        Set<String> writes = CollectionSelectors.WRITES;
        Set<String> reads = CollectionSelectors.READS;
        Set<String> exempt = CollectionSelectors.EXEMPT.keySet();
        List<String> unclassified = new ArrayList<>();
        List<String> twice = new ArrayList<>();
        for (String path : postOrDelete) {
            int in = (writes.contains(path) ? 1 : 0) + (reads.contains(path) ? 1 : 0) + (exempt.contains(path) ? 1 : 0);
            if (in == 0) {
                unclassified.add(path);
            } else if (in > 1) {
                twice.add(path);
            }
        }
        assertThat(unclassified).as("POST or DELETE routes of the table in none of WRITES, READS and EXEMPT").isEmpty();
        assertThat(twice).as("routes in more than one of WRITES, READS and EXEMPT").isEmpty();

        Set<String> stale = new TreeSet<>();
        for (Set<String> set : List.of(writes, reads, exempt)) {
            for (String path : set) {
                if (!postOrDelete.contains(path)) {
                    stale.add(path);
                }
            }
        }
        assertThat(stale).as("classified routes that are not POST or DELETE routes of the table").isEmpty();
        CollectionSelectors.EXEMPT.forEach((path, reason) -> assertThat(reason).as("the reason of exempt %s", path).isNotBlank());
        CollectionSelectors.ROUTES.forEach((path, route) ->
            assertThat(route.writes()).as("the table's writes flag of %s", path).isEqualTo(writes.contains(path)));

        // 3. the rest of the router names no collection
        Set<String> notCollectionRoutes = new TreeSet<>(dispatched.keySet());
        notCollectionRoutes.removeAll(CollectionSelectors.ROUTES.keySet());

        System.out.printf("collection route classification: contexts=%d dispatched=%d table=%d postOrDelete=%d writes=%d reads=%d "
                + "exempt=%d notACollectionRoute=%d queryHandlerBodiesRead=%d unresolvedContexts=%d%n",
            router.contexts(), dispatched.size(), CollectionSelectors.ROUTES.size(), postOrDelete.size(), writes.size(), reads.size(),
            exempt.size(), notCollectionRoutes.size(), queryBodiesRead, router.unresolved());
        assertThat(postOrDelete.size()).as("every POST or DELETE route is classified once")
            .isEqualTo(writes.size() + reads.size() + exempt.size());
        assertThat(notCollectionRoutes).as("non-vacuity: dispatched routes with no collection field").hasSizeGreaterThanOrEqualTo(150);
    }

    @Test
    void theScanSeesAnUnclassifiedRoute() {
        String handler = """
            final class H {
                void handle(HttpExchange ex) {
                    switch (op) {
                        case "/new-writer" -> handleNewWriter(ex, tenant, method);
                        case "/reader" -> handleReader(ex, tenant, method);
                    }
                }
                private void handleNewWriter(HttpExchange ex, String tenant, String method) {
                    if (!"POST".equals(method)) { return; }
                    if (x) { y(); }
                }
                private void handleReader(HttpExchange ex, String tenant, String method) {
                    if (!"GET".equals(method)) { return; }
                }
            }
            """;
        Matcher cases = CASE_ROUTE.matcher(blank(handler));
        List<String> methods = new ArrayList<>();
        while (cases.find()) {
            methods.add(cases.group(1) + "->" + cases.group(2));
        }
        assertThat(methods).containsExactly("/new-writer->handleNewWriter", "/reader->handleReader");
        assertThat(body(blank(handler), "handleNewWriter")).contains("\"POST\"").doesNotContain("handleReader");
        assertThat(body(blank(handler), "handleReader")).contains("\"GET\"").doesNotContain("\"POST\"");
        assertThat(blank("a // case \"/gone\" -> x(\nb /* case \"/gone2\" -> y( */ c")).doesNotContain("/gone");
    }
}
