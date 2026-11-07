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
import static org.assertj.core.api.Assertions.assertThatThrownBy;

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
        return textFunctionsFromPgProc(pg, "nexus");
    }

    /** The result-shape derivation over any schema (the negative control plants a function in its own). */
    static Map<String, String> textFunctionsFromPgProc(PostgreSQLContainer<?> pg, String schema) throws Exception {
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
                .where(DSL.field(DSL.name("n", "nspname"), String.class).eq(schema))
                .fetch();
            for (var r : rows) {
                String res = r.get(result);
                if (res != null && resultCarriesChunkText(res)) out.put(r.get(name), r.get(args));
            }
        }
        return out;
    }

    /** True when a function's result type names a {@code content} or {@code chunk_text} text column. */
    static boolean resultCarriesChunkText(String result) {
        return TEXT_COLUMN.matcher(result).find();
    }

    /**
     * nexus-rfvyy.7.17: the second derivation. The result-shape matcher above sees only a result column NAMED {@code
     * content} or {@code chunk_text}; a function that returns the text as {@code body} or {@code snippet} would slip
     * past it and out of the exclude-argument check. So also list every function of the schema whose definition
     * mentions the {@code chunk_text} column (function name to result type), whatever it calls its result column.
     */
    static Map<String, String> functionsMentioningChunkText(PostgreSQLContainer<?> pg, String schema) throws Exception {
        Map<String, String> out = new TreeMap<>();
        try (Connection su = pg.createConnection("")) {
            var rows = DSL.using(su, SQLDialect.POSTGRES).fetch(
                "select p.proname, pg_get_function_result(p.oid) as res from pg_catalog.pg_proc p "
                + "join pg_catalog.pg_namespace n on n.oid = p.pronamespace "
                + "where n.nspname = ? and p.prokind in ('f', 'p') "
                + "and pg_get_functiondef(p.oid) ~ '\\mchunk_text\\M'", schema);
            for (var r : rows) out.put(r.get("proname", String.class), String.valueOf(r.get("res", String.class)));
        }
        return out;
    }

    /**
     * The functions that mention {@code chunk_text} in their definition but are neither in the result-shape set nor
     * classified (as returning no chunk text) in {@code classified}: each must be classified or covered by the
     * exclude-argument check.
     */
    static Map<String, String> unclassifiedDefinitionReaders(Map<String, String> byDefinition, Set<String> byShape,
                                                             Set<String> classified) {
        Map<String, String> out = new TreeMap<>(byDefinition);
        out.keySet().removeAll(byShape);
        out.keySet().removeAll(classified);
        return out;
    }

    // ── pg_proc ───────────────────────────────────────────────────────────────

    @Test
    void theResultShapeMatcherFiresOnTheColumnNamesItClaimsAndOnlyThose() {
        assertThat(resultCarriesChunkText("TABLE(chash bytea, content text, distance double precision)")).isTrue();
        assertThat(resultCarriesChunkText("TABLE(chash bytea, chunk_text text)")).isTrue();
        assertThat(resultCarriesChunkText("TABLE(content text)")).as("first column").isTrue();
        assertThat(resultCarriesChunkText("TABLE(chash bytea, chunk_text_hash text)"))
            .as("a hash column is not chunk text").isFalse();
        assertThat(resultCarriesChunkText("TABLE(chash bytea, body text)"))
            .as("a text column under another name is invisible to the shape matcher: the definition scan covers it")
            .isFalse();
    }

    @Test
    void aFunctionThatReturnsChunkTextUnderAnotherColumnNameIsCaughtByTheDefinitionScan() throws Exception {
        String probe = "census_probe";
        try (Connection su = pg.createConnection(""); var st = su.createStatement()) {
            st.execute("drop schema if exists " + probe + " cascade");
            st.execute("create schema " + probe);
            st.execute("create function " + probe + ".leak(p text) returns table(chash bytea, body text) "
                + "language plpgsql as $f$ begin return query select c.chash, c.chunk_text from nexus.chunks_384 c; "
                + "end $f$");
        }
        try {
            Map<String, String> byShape = textFunctionsFromPgProc(pg, probe);
            Map<String, String> byDefinition = functionsMentioningChunkText(pg, probe);
            assertThat(byShape).as("the planted function returns chunk text as 'body': the shape matcher misses it")
                .doesNotContainKey("leak");
            assertThat(byDefinition).as("the definition scan sees the planted function").containsKey("leak");
            assertThat(unclassifiedDefinitionReaders(byDefinition, byShape.keySet(), Set.of()))
                .as("so the cross-check names it").containsOnlyKeys("leak");
        } finally {
            try (Connection su = pg.createConnection(""); var st = su.createStatement()) {
                st.execute("drop schema if exists " + probe + " cascade");
            }
        }
    }

    /**
     * Functions whose definition mentions the {@code chunk_text} column but whose result carries no chunk text, with
     * the reason. An entry here is a claim a reviewer can read.
     */
    static final Map<String, String> DEFINITION_READERS_RETURNING_NO_TEXT = Map.ofEntries(
        Map.entry("gc_quarantine_orphans", "moves reapable chunks (no live owner, never sensitive) to quarantine; "
            + "returns a count and a jsonb sample labelled by left(chunk_text, 80)"),
        Map.entry("gc_quarantine_orphans_bounded", "the bounded form of gc_quarantine_orphans: the same sample labels"),
        Map.entry("gc_restore_rereferenced", "INSERT..SELECT of quarantined rows back to the live collection; returns "
            + "a bigint count"),
        Map.entry("gc_restore_rereferenced_bounded", "the bounded form: returns restored and remaining counts"),
        Map.entry("manifest_qualifying_hits", "text gate over manifest chunks; returns hit, collection and chash "
            + "key, no text"),
        Map.entry("quarantine_restore_chunks", "moves quarantine rows back; returns verdict rows (chash, outcome, "
            + "owner id and titles, counts), no chunk text"),
        Map.entry("reaper_quarantine_chunks", "moves reapable chunks to quarantine; returns counts and a refusal flag"),
        Map.entry("text_gate_probe_384", "text gate; SETOF bytea, the chashes whose text matches, no text"),
        Map.entry("text_gate_probe_768", "text gate; SETOF bytea, the chashes whose text matches, no text"),
        Map.entry("text_gate_probe_1024", "text gate; SETOF bytea, the chashes whose text matches, no text"));

    @Test
    void everyNexusFunctionThatMentionsChunkTextIsInTheShapeSetOrClassified() throws Exception {
        Map<String, String> byDefinition = functionsMentioningChunkText(pg, "nexus");
        assertThat(byDefinition.keySet()).as("non-vacuity: the definition scan sees the search families")
            .containsAll(textFunctionArgs.keySet());
        assertThat(unclassifiedDefinitionReaders(byDefinition, textFunctionArgs.keySet(),
                                                 DEFINITION_READERS_RETURNING_NO_TEXT.keySet()))
            .as("nexus functions whose definition reads chunk_text, returned under a column name the shape matcher "
                + "does not know: add the name to it, or classify the function as returning no text")
            .isEmpty();
    }

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

    /**
     * {@code PgVectorRepository} methods that read chunk text (so they are sinks of the census above) but that no
     * route reaches and no main source calls, with the reason. The route census only judges a sink a route reaches,
     * so a method like this one is invisible to it: it takes no {@code excludeSensitive}, and the day a route or any
     * caller wires it in, it would serve sensitive text. The test below pins the premise (still a sink, reached by no
     * route, named by no main source) so that wiring it in fails here and the author must gate it first (review fix F4,
     * the way {@link #NO_ROUTE_FUNCTIONS} pins {@code document_text}).
     */
    static final Map<String, String> NO_CALLER_SINKS = Map.of(
        "fetchDocumentChunks", "a document's chunk text in manifest order (RDR-108); public, no flag, no main caller "
            + "(only tests call it)");

    @Test
    void theSinksExemptedForHavingNoCallerAreStillSinksReachedByNoRouteAndCalledByNoMainSource() throws Exception {
        String repoSrc = Files.readString(VECTOR_REPO);
        Set<String> sinks = ChunkTextRouteCensus.allSinks(repoSrc, textFunctionArgs.keySet());
        Set<String> reachedByRoutes = new TreeSet<>();
        for (var f : findings()) {
            reachedByRoutes.addAll(f.sinkMethods());
            reachedByRoutes.addAll(f.exemptSinksReached());
        }
        assertThat(NO_CALLER_SINKS).as("non-vacuity").isNotEmpty();
        int files = 0;
        for (String method : NO_CALLER_SINKS.keySet()) {
            assertThat(sinks).as("%s still reads chunk text: the exemption has ground", method).contains(method);
            assertThat(reachedByRoutes).as("%s is now reached by a route: gate it with excludeSensitive and "
                + "remove the exemption", method).doesNotContain(method);
            // a call, or a method reference (this::m, Repo::m); comments and string literals are blanked first
            Pattern use = Pattern.compile("\\b" + method + "\\s*\\(|::\\s*" + method + "\\b");
            try (var walk = Files.walk(MAIN)) {
                for (Path p : (Iterable<Path>) walk.filter(f -> f.toString().endsWith(".java"))::iterator) {
                    files++;
                    int uses = (int) use.matcher(KeyedRoutesCensusTest.blank(Files.readString(p))).results().count();
                    // the declaration is the one occurrence in the repository itself
                    int allowed = p.equals(VECTOR_REPO) ? 1 : 0;
                    assertThat(uses).as("%s: main source %s uses %s, which serves chunk text with no sensitivity "
                        + "flag", method, p, method).isEqualTo(allowed);
                }
            }
        }
        assertThat(files).as("non-vacuity: the walk read the engine sources").isGreaterThan(100);
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

    static {
        // found when the census began reading string literals and field initialisers (nexus-rfvyy.7.17)
        OTHER_TEXT_READERS.put("CatalogRepository.java#chunkRow", "map builder resolveSpan (GATED above) calls with "
            + "the values it read; it reads nothing itself");
        OTHER_TEXT_READERS.put("DimTables.java#<type ChunkTable>", "the column handle of the chunk tables "
            + "(t.field(\"chunk_text\")): a reference, not a read; every query that selects it is a method this "
            + "census judges");
        OTHER_TEXT_READERS.put("PipelineHandler.java#handleChunks", "request validation of the chunk_text key of an "
            + "uploaded pdf chunk: input to a write of pdf_chunks, not a read");
    }

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
            assertThat(repoTexts.stream().anyMatch(ChunkTextRouteCensus::usesFlag))
                .as("%s uses %s", name, ChunkTextRouteCensus.FLAG).isTrue();
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

    // ── nexus-rfvyy.7.17: literals are read, and the flag must be USED ─────────

    private static ChunkTextRouteCensus.Finding freshOf(String freshBody, Set<String> textFunctions) {
        String repo = "final class R {\n    List fresh(String a) { " + freshBody + " }\n}\n";
        return ChunkTextRouteCensus.census(HANDLER, repo, textFunctions, Set.of()).stream()
            .filter(f -> f.route().equals("/brand-new")).findFirst().orElseThrow();
    }

    @Test
    void aRawSqlReadOfChunkTextInsideAStringLiteralIsDetected() {
        assertThat(freshOf("return ctx.fetch(\"select chash, chunk_text from nexus.chunks\");", Set.of())
            .readsChunkText()).as("select chunk_text in a string literal").isTrue();
        assertThat(freshOf("return ctx.fetch(\"\"\"\n            select left(chunk_text, 80)\n"
            + "            from nexus.chunks\n            \"\"\");", Set.of())
            .readsChunkText()).as("select chunk_text in a text block").isTrue();
        assertThat(freshOf("return ctx.fetch(\"select * from nexus.plain_search_384(?, ?)\");",
                           Set.of("plain_search_384")).readsChunkText())
            .as("a raw-SQL call of a function the pg_proc derivation found").isTrue();
    }

    @Test
    void aLiteralThatMerelyResemblesChunkTextIsNotARead() {
        assertThat(freshOf("return ctx.fetch(\"select chunk_text_hash from nexus.chunks\");", Set.of())
            .readsChunkText()).as("chunk_text_hash is a different column").isFalse();
        assertThat(freshOf("// select chunk_text from nexus.chunks\n return ctx.fetch(\"select 1\");", Set.of())
            .readsChunkText()).as("a comment is not a read").isFalse();
        assertThat(freshOf("return ctx.fetch(\"select 1\");", Set.of("plain_search_384"))
            .readsChunkText()).isFalse();
    }

    @Test
    void aRawSqlConstantInAFieldInitialiserFailsTheCensusRatherThanBeingMissed() {
        String repo = """
            final class R {
                private static final String SQL = "select chunk_text from nexus.chunks";
                List fresh(String a) { return ctx.fetch(SQL); }
            }
            """;
        assertThatThrownBy(() -> ChunkTextRouteCensus.census(HANDLER, repo, Set.of(), Set.of()))
            .isInstanceOf(AssertionError.class).hasMessageContaining("field initialiser");
    }

    @Test
    void aParameterThatIsDeclaredButNeverUsedIsNotTheFlag() {
        var unused = ChunkTextRouteCensus.census(HANDLER,
            REPO.replace("PLAIN_SEARCH_384.call(a, excludeSensitive)", "PLAIN_SEARCH_384.call(a)"),
            Set.of("plain_search_384"), Set.of());
        assertThat(unused.stream().filter(f -> f.route().equals("/search")).findFirst().orElseThrow().conforms())
            .as("the method that reads the text declares the flag and never uses it").isFalse();
        var viaPlan = ChunkTextRouteCensus.census(HANDLER,
            REPO.replace("PLAIN_SEARCH_384.call(a, excludeSensitive)", "PLAIN_SEARCH_384.call(a, wherePlan.excludeSensitive())")
                .replace(", boolean excludeSensitive", ""),
            Set.of("plain_search_384"), Set.of());
        assertThat(viaPlan.stream().filter(f -> f.route().equals("/search")).findFirst().orElseThrow().conforms())
            .as("wherePlan.excludeSensitive() is a use").isTrue();
    }

    @Test
    void aHandlerThatParsesTheFlagButNeverPassesItToTheRepositoryFailsTheCensus() {
        var dropped = ChunkTextRouteCensus.census(
            HANDLER.replace("repo.plain(a, excludeSensitive)", "repo.plain(a, true)"), REPO,
            Set.of("plain_search_384"), Set.of());
        assertThat(dropped.stream().filter(f -> f.route().equals("/search")).findFirst().orElseThrow().conforms())
            .as("the parsed flag does not reach the repository call").isFalse();
        var nested = ChunkTextRouteCensus.census(
            HANDLER.replace("repo.plain(a, excludeSensitive)", "repo.plain(a, Boolean.valueOf(excludeSensitive))"),
            REPO, Set.of("plain_search_384"), Set.of());
        assertThat(nested.stream().filter(f -> f.route().equals("/search")).findFirst().orElseThrow().conforms())
            .as("the flag inside a nested call argument still reaches it").isTrue();
    }

    @Test
    void aFunctionTheCensusDoesNotKnowIsNotAChunkTextRead() {
        var findings = ChunkTextRouteCensus.census(HANDLER, REPO, Set.of(), Set.of());
        assertThat(findings.stream().filter(f -> f.route().equals("/search")).findFirst().orElseThrow()
            .readsChunkText()).as("with no text functions derived, PLAIN_SEARCH_384 reads none").isFalse();
    }
}
