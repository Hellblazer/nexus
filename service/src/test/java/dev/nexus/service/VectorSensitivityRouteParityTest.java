// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.TreeSet;
import java.util.regex.Pattern;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-rfvyy.7.4 (RDR-243 Phase 2 Step 2.1b, Test Plan "Route parity"): every engine route that returns chunk text
 * accepts {@code sensitivity} and filters. The list of such routes is DERIVED, never typed:
 * <ol>
 *   <li>from {@code pg_proc}: every {@code nexus} function whose result row carries chunk text must end in {@code
 *       p_exclude_sensitive} (a function added later joins the set by its result shape);</li>
 *   <li>from the route table: {@link ChunkTextRouteCensus} reads {@code VectorHandler}'s dispatch, follows each
 *       handler into {@code PgVectorRepository}, and calls a route a chunk-text route when a reachable method
 *       selects {@code chunk_text} (the jOOQ reads {@code pg_proc} cannot see: get-by-id) or calls one of the
 *       functions of step 1. Each such route's handler must parse {@code sensitivity} and each method that reads the
 *       text must carry {@code excludeSensitive};</li>
 *   <li>a sink census over the other engine sources: a method that reads chunk text and is not reached by a
 *       vector route must be either gated (the catalog by-address routes) or named below with the reason it serves
 *       no read route, so a new text read cannot slip in unclassified.</li>
 * </ol>
 * The behaviour half (an {@code exclude} request drops a planted sensitive row on each derived route) is in {@code
 * VectorSensitivityExcludeIntegrationTest}, which also asserts it holds a recipe for every route derived here.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class VectorSensitivityRouteParityTest {

    private static final Path MAIN = Path.of("src/main/java/dev/nexus/service");
    private static final Path VECTOR_HANDLER = MAIN.resolve("http/VectorHandler.java");
    private static final Path VECTOR_REPO = MAIN.resolve("vectors/PgVectorRepository.java");
    private static final Pattern TEXT_COLUMN = Pattern.compile("[(,]\\s*(content|chunk_text)\\s+text\\b");

    PostgreSQLContainer<?> pg;
    /** function name to its argument list, for every nexus function whose result row carries chunk text. */
    Map<String, String> textFunctionArgs;

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        textFunctionArgs = textFunctionsFromPgProc(pg);
    }

    @AfterAll
    void stop() {
        if (pg != null) pg.stop();
    }

    /** Every {@code nexus} function whose result row carries chunk text, to its argument list, from {@code pg_proc}. */
    static Map<String, String> textFunctionsFromPgProc(PostgreSQLContainer<?> pg) throws Exception {
        var proc = DSL.table(DSL.name("pg_catalog", "pg_proc")).as("p");
        var nsp = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
        Field<String> name = DSL.field(DSL.name("p", "proname"), String.class);
        Field<Object> oid = DSL.field(DSL.name("p", "oid"), Object.class);
        Field<String> args = DSL.function("pg_get_function_arguments", String.class, oid);
        Field<String> result = DSL.function("pg_get_function_result", String.class, oid);
        Map<String, String> out = new TreeMap<>();
        try (Connection su = pg.createConnection("")) {
            var rows = DSL.using(su, SQLDialect.POSTGRES)
                .select(name, args, result).from(proc)
                .join(nsp).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("p", "pronamespace"))))
                .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
                .fetch();
            for (var r : rows) {
                String res = r.get(result);
                if (res != null && TEXT_COLUMN.matcher(res).find()) out.put(r.get(name), r.get(args));
            }
        }
        return out;
    }

    // ── pg_proc ───────────────────────────────────────────────────────────────

    @Test
    void everyNexusFunctionThatReturnsChunkTextEndsInTheExcludeArgument() {
        assertThat(textFunctionArgs.keySet())
            .as("non-vacuity: the 10 search families x 3 dimensions return chunk text")
            .hasSizeGreaterThanOrEqualTo(27);
        List<String> missing = new java.util.ArrayList<>();
        for (var e : textFunctionArgs.entrySet()) {
            if (NO_ROUTE_FUNCTIONS.containsKey(e.getKey())) continue;
            if (!e.getValue().endsWith("p_exclude_sensitive boolean DEFAULT false")) {
                missing.add(e.getKey() + "(" + e.getValue() + ")");
            }
        }
        assertThat(missing)
            .as("a nexus function returning chunk text without the trailing p_exclude_sensitive argument")
            .isEmpty();
    }

    /**
     * SQL functions that return chunk text and that no engine route calls: a client with a database login can call
     * them, the HTTP service never does. The test below pins that premise (no engine source names the function), so
     * wiring one into a route removes the exemption's ground and the route census then judges that route.
     */
    static final Map<String, String> NO_ROUTE_FUNCTIONS = Map.of(
        "document_text", "ordered manifest-chunk join of one document (catalog-004); no engine caller");

    @Test
    void theFunctionsExemptedFromTheArgumentAreNamedByNoEngineSource() throws Exception {
        for (String fn : NO_ROUTE_FUNCTIONS.keySet()) {
            assertThat(textFunctionArgs).as("the exemption names a function that still returns chunk text")
                .containsKey(fn);
            try (var files = Files.walk(MAIN)) {
                for (Path p : (Iterable<Path>) files.filter(f -> f.toString().endsWith(".java"))::iterator) {
                    String src = KeyedRoutesCensusTest.blank(Files.readString(p));
                    assertThat(src).as("%s must not call %s", p, fn)
                        .doesNotContain(fn.toUpperCase(java.util.Locale.ROOT) + ".call");
                }
            }
        }
    }

    /** The by-address catalog routes that return chunk text: gated like the vector reads, checked by name below. */
    static final String GATED = "GATED";

    /**
     * Methods outside PgVectorRepository that read chunk text, and why each is not an ungated read route. {@link
     * #GATED} marks the two catalog by-address lookups (resolve_span, resolve_chash): they return chunk text, take
     * {@code sensitivity} and carry the flag, which the test below checks in the method and in its handler.
     */
    static final Map<String, String> OTHER_TEXT_READERS = new TreeMap<>(Map.of(
        "CatalogRepository.java#resolveSpan", GATED,
        "CatalogRepository.java#resolveChash", GATED,
        "CatalogRepository.java#upsertManifestChunkVectors", "write: sets chunk_text on the manifest upsert",
        "ChunkOwnerLabel.java#textPrefix", "left(chunk_text, 80) label of chunks reapable(c) selects (no live owner, "
            + "so never sensitive: chunk_sensitive counts live owners only) and of refusal audit rows",
        "ReaperRepository.java#describe", "80-char labels of the chunks a gc sweep selected or refused to move: "
            + "reapable(c) chunks, which have no live owner, in an operator audit row",
        "VectorHandler.java#handleReapable", "serves PgVectorRepository#reapableChunks, an exempt sink above",
        "CombinedWriteService.java#selectExistingText", "write: compares stored text, returns nothing",
        "PipelineRepository.java#writeChunks", "write: pdf_chunks, a different table",
        "ReembedSourceReader.java#chunks", "internal re-embed job input; the job route returns state and counts",
        "ReembedSourceReader.java#planCounts", "octet_length of the text: byte counts"));

    // ── route table ───────────────────────────────────────────────────────────

    private List<ChunkTextRouteCensus.Finding> findings() throws Exception {
        return findings(textFunctionArgs.keySet());
    }

    /** The census over the real sources; the behaviour test uses it to check it holds a recipe per derived route. */
    static List<ChunkTextRouteCensus.Finding> findings(Set<String> textFunctions) throws Exception {
        return ChunkTextRouteCensus.census(Files.readString(VECTOR_HANDLER), Files.readString(VECTOR_REPO),
                                           textFunctions, EXEMPT_SINKS.keySet());
    }

    /** A repository method that touches the text without serving it, and the one route that may reach it. */
    record Exempt(String route, String reason) { }

    /**
     * Repository methods that touch {@code chunk_text} without returning a chunk a live owner could have marked
     * sensitive. They are not chunk-text reads in the sense of this test; each is checked below to still be a sink
     * and to be reached by exactly the route named, so the entry cannot rot into a hole.
     */
    static final Map<String, Exempt> EXEMPT_SINKS = Map.of(
        "upsertChunksInternal", new Exempt("/upsert-chunks", "sets chunk_text on insert and conflict; returns ids"),
        "selectExistingChashTextCtx", new Exempt("/upsert-chunks",
            "reads stored text only to decide whether an id needs re-embedding"),
        "reapableChunks", new Exempt("/reapable", "lists the chunks reapable(c) selects, which have no live owner "
            + "and so are never sensitive (chunk_sensitive counts live owners only); text is an 80-char label"));

    /** The routes the census derives as chunk-text routes, for the integration test's coverage check. */
    static Set<String> derivedTextRoutes(List<ChunkTextRouteCensus.Finding> findings) {
        Set<String> out = new TreeSet<>();
        for (var f : findings) if (f.readsChunkText()) out.add("/v1/vectors" + f.route());
        return out;
    }

    @Test
    void everyDerivedChunkTextRouteParsesSensitivityAndFiltersInTheMethodThatReadsTheText() throws Exception {
        var findings = findings();
        Set<String> routes = derivedTextRoutes(findings);
        // non-vacuity: the eight searches (query is an alias of search), get and store-get
        assertThat(routes).as("derived chunk-text routes").contains(
            "/v1/vectors/search", "/v1/vectors/query", "/v1/vectors/search-per-collection",
            "/v1/vectors/hybrid-search", "/v1/vectors/search-metadata-scoped", "/v1/vectors/search-topic-scoped",
            "/v1/vectors/search-graph-hop", "/v1/vectors/search-aspect-scoped",
            "/v1/vectors/get", "/v1/vectors/store-get");
        Map<String, String> nonConforming = new LinkedHashMap<>();
        for (var f : findings) {
            if (f.readsChunkText() && !f.conforms()) {
                nonConforming.put(f.route(), "handler " + f.handler() + " parses sensitivity="
                    + f.handlerParsesSensitivity() + "; reads chunk text in " + f.sinkMethods()
                    + " without " + ChunkTextRouteCensus.FLAG + " in " + f.sinkMethodsWithoutFlag());
            }
        }
        assertThat(nonConforming).as("chunk-text routes that do not accept sensitivity and filter").isEmpty();
    }

    @Test
    void theExemptSinksAreStillSinksAndEachIsReachedByExactlyItsNamedRoute() throws Exception {
        Set<String> sinks = ChunkTextRouteCensus.allSinks(Files.readString(VECTOR_REPO), textFunctionArgs.keySet());
        assertThat(sinks).containsAll(EXEMPT_SINKS.keySet());
        Map<String, Set<String>> routesBySink = new TreeMap<>();
        for (var f : findings()) {
            for (String s : f.exemptSinksReached()) routesBySink.computeIfAbsent(s, k -> new TreeSet<>()).add(f.route());
        }
        for (var e : EXEMPT_SINKS.entrySet()) {
            assertThat(routesBySink.getOrDefault(e.getKey(), Set.of()))
                .as("routes reaching the exempt sink %s", e.getKey()).containsExactly(e.getValue().route());
        }
    }

    @Test
    void theTwoCatalogByAddressReadsAreGatedInTheMethodAndTheirHandler() throws Exception {
        String repoSrc = Files.readString(MAIN.resolve("db/CatalogRepository.java"));
        String handlerSrc = Files.readString(MAIN.resolve("http/CatalogHandler.java"));
        int gated = 0;
        for (var e : OTHER_TEXT_READERS.entrySet()) {
            if (!e.getValue().equals(GATED)) continue;
            gated++;
            String name = e.getKey().substring(e.getKey().indexOf('#') + 1);
            String handlerName = "handle" + Character.toUpperCase(name.charAt(0)) + name.substring(1);
            var repoTexts = ChunkTextRouteCensus.memberTexts(repoSrc, name);
            assertThat(repoTexts).as("%s exists", name).isNotEmpty();
            assertThat(repoTexts.stream().anyMatch(t -> t.contains(ChunkTextRouteCensus.FLAG)))
                .as("%s carries %s", name, ChunkTextRouteCensus.FLAG).isTrue();
            var handlerTexts = ChunkTextRouteCensus.memberTexts(handlerSrc, handlerName);
            assertThat(handlerTexts).as("%s exists", handlerName).isNotEmpty();
            assertThat(handlerTexts.stream().allMatch(t -> t.contains("ChunkSensitivity.excludeFlag")))
                .as("%s parses sensitivity", handlerName).isTrue();
        }
        assertThat(gated).isEqualTo(2);
    }

    @Test
    void everyOtherMethodThatReadsChunkTextIsGatedOrClassified() throws Exception {
        Set<String> found = new TreeSet<>();
        int files = 0;
        try (var walk = Files.walk(MAIN)) {
            for (Path p : (Iterable<Path>) walk.filter(f -> f.toString().endsWith(".java"))::iterator) {
                if (p.equals(VECTOR_REPO)) continue;
                files++;
                found.addAll(ChunkTextRouteCensus.textReaders(p.getFileName().toString(),
                                                              Files.readString(p), textFunctionArgs.keySet()));
            }
        }
        assertThat(files).as("non-vacuity: the sink census read the engine sources").isGreaterThan(100);
        assertThat(found).as("methods outside PgVectorRepository that read chunk text: classify each in "
            + "OTHER_TEXT_READERS or gate it").containsExactlyInAnyOrderElementsOf(OTHER_TEXT_READERS.keySet());
    }

    @Test
    void theRoutesThatReadNoChunkTextAreTheOnesThatReturnNone() throws Exception {
        // the complement is derived too; this pins the two read routes that return metadata only, so a change that
        // makes one of them return text moves it into the set above instead of past it
        Set<String> none = new TreeSet<>();
        for (var f : findings()) if (!f.readsChunkText()) none.add(f.route());
        assertThat(none).contains("/get-all-metadata", "/store-list", "/count", "/collections");
    }

    // ── the census must catch what it exists to catch ─────────────────────────

    private static final String HANDLER = """
        final class H {
            public void handle(HttpExchange ex) {
                switch (op) {
                    case "/search" -> handleSearch(ex, m);
                    case "/brand-new" -> handleBrandNew(ex, m);
                }
            }
            private void handleSearch(HttpExchange ex, String m) {
                boolean excludeSensitive = optExcludeSensitive(body);
                repo.plain(a, excludeSensitive);
            }
            private void handleBrandNew(HttpExchange ex, String m) {
                repo.fresh(a);
            }
        }
        """;

    private static final String REPO = """
        final class R {
            List plain(String a, boolean excludeSensitive) { return PLAIN_SEARCH_384.call(a, excludeSensitive); }
            List fresh(String a) { return ctx.select(ch.chash(), ch.chunkText()).from(ch.table()).fetch(); }
        }
        """;

    @Test
    void aNewChunkTextRouteWithoutTheParameterFailsTheCensus() {
        var findings = ChunkTextRouteCensus.census(HANDLER, REPO, Set.of("plain_search_384"), Set.of());
        var search = findings.stream().filter(f -> f.route().equals("/search")).findFirst().orElseThrow();
        var fresh = findings.stream().filter(f -> f.route().equals("/brand-new")).findFirst().orElseThrow();
        assertThat(search.readsChunkText()).isTrue();
        assertThat(search.conforms()).as("the search route has the parameter at both ends").isTrue();
        assertThat(fresh.readsChunkText()).as("a jOOQ read of chunk_text is a chunk-text route").isTrue();
        assertThat(fresh.conforms()).as("and without the parameter it does not conform").isFalse();
        assertThat(fresh.handlerParsesSensitivity()).isFalse();
        assertThat(fresh.sinkMethodsWithoutFlag()).containsExactly("fresh");
    }

    @Test
    void aHandlerThatDropsTheParseOrARepositoryThatDropsTheFlagFailsTheCensus() {
        var noParse = ChunkTextRouteCensus.census(
            HANDLER.replace("optExcludeSensitive(body)", "false"), REPO, Set.of("plain_search_384"), Set.of());
        assertThat(noParse.stream().filter(f -> f.route().equals("/search")).findFirst().orElseThrow().conforms())
            .as("the route no longer parses sensitivity").isFalse();
        var noFlag = ChunkTextRouteCensus.census(
            HANDLER, REPO.replace(", boolean excludeSensitive", "").replace(", excludeSensitive", ""),
            Set.of("plain_search_384"), Set.of());
        assertThat(noFlag.stream().filter(f -> f.route().equals("/search")).findFirst().orElseThrow().conforms())
            .as("the method that reads the text no longer carries the flag").isFalse();
    }

    @Test
    void aReceiverOtherThanRepoIsNotFollowed() {
        String handler = """
            final class H {
                public void handle(HttpExchange ex) { switch (op) { case "/count" -> handleCount(ex, m); } }
                private void handleCount(HttpExchange ex, String m) { var x = body.get("a"); repo.count(a); }
            }
            """;
        String repo = """
            final class R {
                long count(String a) { return 1; }
                Map get(String a) { return ctx.select(ch.chunkText()).fetch(); }
            }
            """;
        var f = ChunkTextRouteCensus.census(handler, repo, Set.of(), Set.of()).get(0);
        assertThat(f.readsChunkText()).as("body.get(...) is not repo.get(...)").isFalse();
    }

    @Test
    void aFunctionTheCensusDoesNotKnowIsNotAChunkTextRead() {
        var findings = ChunkTextRouteCensus.census(HANDLER, REPO, Set.of(), Set.of());
        assertThat(findings.stream().filter(f -> f.route().equals("/search")).findFirst().orElseThrow()
            .readsChunkText()).as("with no text functions derived, PLAIN_SEARCH_384 reads none").isFalse();
    }
}
