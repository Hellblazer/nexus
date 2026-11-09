// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.PgContainerHelper;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-8ptg4 review S1: the manifest predicate's per-occurrence object is written out in every SQL function that
 * evaluates it, and the copies must be the same text. A key added to one and not the others is not caught by a
 * behavioural matrix unless the matrix happens to name that key; {@code manifest_qualifying_hits} (vectors-047) is
 * the case that matters most, because its count must NEVER be lower than what {@code manifest_search_<dim>} admits,
 * and a copy that fell behind would under-count and skip a re-run a starved page needs.
 *
 * <p>The test reads each function's stored source ({@code pg_proc.prosrc}), cuts out the object block (the {@code
 * CROSS JOIN LATERAL} that derives the source format, through the closing {@code ) e} of the lateral that builds
 * {@code eff}), drops SQL comments, collapses whitespace, and asserts every block is the same string. Whitespace and
 * comments may differ between functions; a key, a fallback, a test or a function name may not.
 *
 * <p>THE ONE DECLARED DIFFERENCE (nexus-6pfgb.41; nexus-u9dc5 adds the route (c) body). The nine route (b) bodies
 * ({@code search_metadata_scoped_<dim>}, {@code search_aspect_scoped_<dim>}, {@code search_graph_hop_<dim>}) and the
 * route (c) body ({@code manifest_admits_chunk}, the 14th) carry the same object except for the {@code
 * content_type} key. {@code manifest_search} over-admits on that key because route (a)'s stage judges it exactly
 * afterwards; route (b) has no stage, so its words are the stage's exact rule ({@code ManifestLabelStage#matches}). The
 * key is cut out of every block (from {@code 'content_type', CASE} to {@code 'tags', CASE}); the rest must be equal in
 * all fourteen, and the cut text must be the reference's in the route (a) four and {@link #ROUTE_B_CONTENT_TYPE} in the
 * route (b) nine and the route (c) one. Nothing else may differ.
 *
 * <p>TO COVER ANOTHER FUNCTION add its name to {@link #ROUTE_A}, {@link #ROUTE_B} or {@link #ROUTE_C}. The extractor ({@link
 * #objectBlock}) and the comparison ({@link #disagreements}) are static and take plain strings, so they can be
 * pointed at any source text.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ManifestObjectParityTest {

    /** The route (a) copies. The first is the reference the others are held to. */
    static final List<String> ROUTE_A = List.of(
        "manifest_search_384", "manifest_search_768", "manifest_search_1024", "manifest_qualifying_hits");

    /** The route (b) copies (nexus-6pfgb.41): their {@code content_type} key is the stage's exact rule. */
    static final List<String> ROUTE_B = List.of(
        "search_metadata_scoped_384", "search_metadata_scoped_768", "search_metadata_scoped_1024",
        "search_aspect_scoped_384", "search_aspect_scoped_768", "search_aspect_scoped_1024",
        "search_graph_hop_384", "search_graph_hop_768", "search_graph_hop_1024");

    /**
     * The route (c) copy (nexus-u9dc5): {@code nexus.manifest_admits_chunk}, which the get and get-all-metadata routes
     * call. Route (c) has no stage after the read either, so its {@code content_type} key is route (b)'s exact form.
     */
    static final List<String> ROUTE_C = List.of("manifest_admits_chunk");

    /** Every function that carries the per-occurrence object. */
    static final List<String> FUNCTIONS = java.util.stream.Stream.of(ROUTE_A, ROUTE_B, ROUTE_C)
        .flatMap(List::stream).toList();

    private static final String CONTENT_TYPE_START = "'content_type', CASE WHEN nexus.manifest_names_key(";
    private static final String TAGS_START = "'tags', CASE WHEN nexus.manifest_names_key(";
    private static final String CONTENT_TYPE_MARK = "<content_type>";

    /**
     * The {@code manifest_names_key} calls of the object: one per key, 17 until nexus-qpt52; then six more keys
     * (bib_year, bib_authors, bib_venue, bib_citation_count, indexed_at and content_type_shown) in a second object, each
     * named once inside it and once more in the CASE that builds the second object only when one of them is named, 17 +
     * 6 + 6. A block that names fewer than this has been cut in the wrong place.
     */
    private static final int MIN_NAMED_KEYS = 29;

    private static final Pattern LINE_COMMENT = Pattern.compile("--[^\n]*");
    private static final Pattern BLOCK_COMMENT = Pattern.compile("/\\*.*?\\*/", Pattern.DOTALL);
    private static final Pattern WHITESPACE = Pattern.compile("\\s+");
    private static final Pattern BLOCK_START =
        Pattern.compile("CROSS\\s+JOIN\\s+LATERAL\\s*\\(\\s*SELECT\\s+nexus\\.catalog_source_format");
    private static final Pattern BLOCK_END = Pattern.compile("\\)\\s+AS\\s+eff\\s*\\)\\s+e\\b");

    /**
     * The route (b) {@code content_type} key, normalised: the words the document answers to under {@code
     * ManifestLabelStage#matches}. The operands {@code pdf} and {@code markdown} read the derived format when there is
     * one, else the chunk row's value; every other operand reads the catalog content type when it is not blank, else
     * the chunk row's value, which is the empty string for a document that has no format either (nexus-qpt52: the stage
     * reads an unlabelled value there, and {@code content_type=""} names it). This is the one place the route (b) object is allowed to differ from {@code
     * manifest_search}'s, and the text is pinned here so that a change to it is a change to this test.
     */
    static final String ROUTE_B_CONTENT_TYPE = normalise(
        "'content_type', CASE WHEN nexus.manifest_names_key(p_manifest_where, p_manifest_path, 'content_type') "
            + "THEN to_jsonb(array_remove(ARRAY[ "
            + "CASE WHEN f.fmt IS NOT NULL "
            + "THEN CASE WHEN f.fmt IN ('pdf', 'markdown') THEN f.fmt END "
            + "ELSE CASE WHEN c.metadata ->> 'content_type' IN ('pdf', 'markdown') THEN c.metadata ->> 'content_type' END END, "
            + "CASE WHEN d.content_type ~ '\\S' "
            + "THEN CASE WHEN d.content_type NOT IN ('pdf', 'markdown') THEN d.content_type END "
            + "ELSE CASE WHEN COALESCE(c.metadata ->> 'content_type', CASE WHEN f.fmt IS NULL THEN '' END) NOT IN ('pdf', 'markdown') "
            + "THEN COALESCE(c.metadata ->> 'content_type', CASE WHEN f.fmt IS NULL THEN '' END) END END "
            + "], NULL)) END,");

    /** Comments removed and whitespace collapsed to single spaces, so layout differences are not differences. */
    static String normalise(String sql) {
        String s = BLOCK_COMMENT.matcher(sql).replaceAll(" ");
        s = LINE_COMMENT.matcher(s).replaceAll(" ");
        return WHITESPACE.matcher(s).replaceAll(" ").trim();
    }

    /**
     * The normalised object block of one function body: from the {@code CROSS JOIN LATERAL} that derives {@code
     * fmt} through the {@code ) e} that closes the lateral building {@code eff}. Throws when either end is missing,
     * so a body that changed shape fails loudly instead of comparing two empty strings.
     */
    static String objectBlock(String prosrc) {
        String body = normalise(prosrc);
        Matcher start = BLOCK_START.matcher(body);
        if (!start.find()) {
            throw new IllegalStateException("no object block start (CROSS JOIN LATERAL ... catalog_source_format)");
        }
        Matcher end = BLOCK_END.matcher(body);
        if (!end.find(start.start())) {
            throw new IllegalStateException("no object block end (') AS eff ) e') after the start");
        }
        return body.substring(start.start(), end.end());
    }

    /** The block with its {@code content_type} key cut out (a marker stays in its place), and the cut text. */
    record Parts(String rest, String contentType) {}

    static Parts parts(String prosrc) {
        String block = objectBlock(prosrc);
        int from = block.indexOf(CONTENT_TYPE_START);
        int to = block.indexOf(TAGS_START);
        if (from < 0 || to <= from) {
            throw new IllegalStateException("no content_type key before the tags key in the object block");
        }
        return new Parts(block.substring(0, from) + CONTENT_TYPE_MARK + block.substring(to),
            block.substring(from, to).trim());
    }

    /**
     * The names (other than the first, the reference) whose block differs from the first's in anything but the {@code
     * content_type} key, which {@link #contentTypeDisagreements} judges.
     */
    static List<String> disagreements(Map<String, String> bodiesByName) {
        List<String> out = new ArrayList<>();
        String referenceName = null;
        String reference = null;
        for (var e : bodiesByName.entrySet()) {
            String block = parts(e.getValue()).rest();
            if (reference == null) {
                referenceName = e.getKey();
                reference = block;
            } else if (!reference.equals(block)) {
                out.add(e.getKey() + " differs from " + referenceName + " at " + firstDifference(reference, block));
            }
        }
        return out;
    }

    /**
     * The functions whose {@code content_type} key is not the one they are declared to carry: the first route (a) copy's
     * for the route (a) four, {@link #ROUTE_B_CONTENT_TYPE} for the route (b) nine and the route (c) one.
     */
    static List<String> contentTypeDisagreements(Map<String, String> bodiesByName) {
        String reference = parts(bodiesByName.get(ROUTE_A.get(0))).contentType();
        List<String> out = new ArrayList<>();
        for (String name : FUNCTIONS) {
            String expected = ROUTE_A.contains(name) ? reference : ROUTE_B_CONTENT_TYPE;
            String actual = parts(bodiesByName.get(name)).contentType();
            if (!expected.equals(actual)) {
                out.add(name + " content_type is not its declared form at " + firstDifference(expected, actual));
            }
        }
        return out;
    }

    // ---- The text gate (nexus-zk7rw review r1, the critic's S2) -------------------------------------------------------

    /**
     * The functions whose text gate must be the same text: the three {@code manifest_search_<dim>} bodies (the gate a
     * hybrid statement applies) and {@code manifest_qualifying_hits}, which applies it again to count. A count whose gate
     * admits less than the search's would under-count, and skip a re-run a starved page needs: a silent recall loss.
     */
    static final List<String> GATED = List.of(
        "manifest_search_384", "manifest_search_768", "manifest_search_1024", "manifest_qualifying_hits");

    private static final String GATE_START = "(p_query_text IS NULL";

    /**
     * The normalised text gate of one function body: the parenthesised group that opens {@code (p_query_text IS NULL},
     * through its matching close. Throws when the group is missing or never closes, so a body that lost or reshaped its
     * gate fails loudly instead of comparing two empty strings.
     */
    static String gateClause(String prosrc) {
        String body = normalise(prosrc);
        int from = body.indexOf(GATE_START);
        if (from < 0) {
            throw new IllegalStateException("no text gate (a group opening '(p_query_text IS NULL')");
        }
        int depth = 0;
        for (int i = from; i < body.length(); i++) {
            char ch = body.charAt(i);
            if (ch == '(') depth++;
            else if (ch == ')' && --depth == 0) {
                return body.substring(from, i + 1);
            }
        }
        throw new IllegalStateException("the text gate group never closes");
    }

    /** The {@link #GATED} names whose gate differs from the first's (the reference, {@code manifest_search_384}). */
    static List<String> gateDisagreements(Map<String, String> bodiesByName) {
        List<String> out = new ArrayList<>();
        String reference = gateClause(bodiesByName.get(GATED.get(0)));
        for (String name : GATED.subList(1, GATED.size())) {
            String gate = gateClause(bodiesByName.get(name));
            if (!reference.equals(gate)) {
                out.add(name + " gate differs from " + GATED.get(0) + " at " + firstDifference(reference, gate));
            }
        }
        return out;
    }

    private static String firstDifference(String a, String b) {
        int i = 0;
        int n = Math.min(a.length(), b.length());
        while (i < n && a.charAt(i) == b.charAt(i)) i++;
        int from = Math.max(0, i - 60);
        return "offset " + i + ": [" + a.substring(from, Math.min(a.length(), i + 60)) + "] vs ["
            + b.substring(from, Math.min(b.length(), i + 60)) + "]";
    }

    PostgreSQLContainer<?> pg;
    private final Map<String, String> bodies = new LinkedHashMap<>();

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        var src = DSL.field(DSL.name("p", "prosrc"), String.class);
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (String proname : FUNCTIONS) {
                List<String> found = ctx.select(src)
                    .from(DSL.table(DSL.name("pg_catalog", "pg_proc")).as("p"))
                    .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n"))
                    .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("p", "pronamespace"))))
                    .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus")
                        .and(DSL.field(DSL.name("p", "proname"), String.class).eq(proname)))
                    .fetch(src);
                assertThat(found).as("non-vacuity: exactly one nexus.%s", proname).hasSize(1);
                bodies.put(proname, found.get(0));
            }
        }
    }

    @AfterAll
    void stop() {
        if (pg != null) pg.stop();
    }

    @Test
    void everyFunctionBuildsTheSameObject_exceptTheDeclaredContentTypeKey() {
        assertThat(bodies.keySet()).as("non-vacuity: all fourteen are compared").containsExactlyElementsOf(FUNCTIONS);
        assertThat(FUNCTIONS).hasSize(14);
        assertThat(disagreements(bodies))
            .as("functions whose per-occurrence object differs from %s", FUNCTIONS.get(0)).isEmpty();
    }

    @Test
    void theContentTypeKey_isTheReferencesOnRouteA_andTheStagesExactRuleOnRouteB() {
        assertThat(contentTypeDisagreements(bodies)).isEmpty();
        // The exception is real and narrow: the two forms are different text, so the declared difference is not vacuous.
        assertThat(ROUTE_B_CONTENT_TYPE).isNotEqualTo(parts(bodies.get(ROUTE_A.get(0))).contentType());
    }

    @Test
    void theExtractedBlockIsTheObject_notAnEmptyOrTruncatedRegion() {
        // Non-vacuity: equal-and-empty would pass the comparison, so each block must be the whole object.
        for (var e : bodies.entrySet()) {
            String block = objectBlock(e.getValue());
            assertThat(parts(e.getValue()).contentType()).as("%s names its content_type", e.getKey())
                .startsWith(CONTENT_TYPE_START).endsWith("END,");
            assertThat(block).as(e.getKey()).startsWith("CROSS JOIN LATERAL ( SELECT nexus.catalog_source_format(")
                .endsWith(") AS eff ) e")
                .contains("catalog_tag_set(").contains("'frecency_score'").contains("'source_author'");
            assertThat(block.split("manifest_names_key\\(", -1).length - 1)
                .as("%s names its keys through manifest_names_key", e.getKey()).isGreaterThanOrEqualTo(MIN_NAMED_KEYS);
        }
    }

    // ---- The check must fail when a copy drifts (the extractor and comparison run on mutated real bodies). ----

    private Map<String, String> withMutated(String proname, String from, String to) {
        Map<String, String> mutated = new LinkedHashMap<>(bodies);
        String body = mutated.get(proname);
        assertThat(body).as("the mutation site exists in %s", proname).contains(from);
        mutated.put(proname, body.replace(from, to));
        return mutated;
    }

    @Test
    void aKeyRenamedInOneCopy_isReported() {
        var mutated = withMutated("manifest_qualifying_hits", "'extraction_method'", "'extraction_methd'");
        assertThat(disagreements(mutated)).as("a renamed key").hasSize(1)
            .allMatch(s -> s.startsWith("manifest_qualifying_hits differs from manifest_search_384"));
    }

    @Test
    void aFallbackDroppedFromTheReference_isReportedAgainstEveryOtherCopy() {
        var mutated = withMutated("manifest_search_384", "COALESCE(d.author, '') ~ '\\S'", "d.author ~ '\\S'");
        assertThat(disagreements(mutated)).as("a changed test in the reference").hasSize(FUNCTIONS.size() - 1);
    }

    @Test
    void aKeyRenamedInARouteBCopy_isReported() {
        var mutated = withMutated("search_aspect_scoped_1024", "'extraction_method'", "'extraction_methd'");
        assertThat(disagreements(mutated)).as("a renamed key").hasSize(1)
            .allMatch(s -> s.startsWith("search_aspect_scoped_1024 differs from manifest_search_384"));
    }

    @Test
    void aRouteBCopyThatDropsATag_isReported() {
        var mutated = withMutated("search_graph_hop_384", "position('\"tags\"' in p_manifest_path) > 0",
            "position('\"tags\"' in p_manifest_path) >= 0");
        assertThat(disagreements(mutated)).hasSize(1);
    }

    @Test
    void aRouteBContentTypeThatDriftsFromTheStagesRule_isReported() {
        var mutated = withMutated("search_graph_hop_768", "d.content_type NOT IN ('pdf', 'markdown')",
            "d.content_type IN ('pdf', 'markdown')");
        assertThat(disagreements(mutated)).as("only the content_type key differs").isEmpty();
        assertThat(contentTypeDisagreements(mutated)).hasSize(1)
            .allMatch(s -> s.startsWith("search_graph_hop_768 content_type"));
    }

    /** {@code target}'s raw text with its {@code content_type} key replaced by {@code donor}'s. */
    private static String withContentTypeOf(String target, String donor) {
        String from = target.substring(0, target.indexOf(CONTENT_TYPE_START));
        String key = donor.substring(donor.indexOf(CONTENT_TYPE_START), donor.indexOf(TAGS_START));
        return from + key + target.substring(target.indexOf(TAGS_START));
    }

    @Test
    void aRouteBCopyOnTheOverAdmittingRouteAForm_isReported() {
        // The shared-chunk defect: a route (b) body that kept manifest_search's content_type admits the chunk row's
        // value for a document whose catalog type is not blank, so a note sharing a chunk with a prose file answers
        // content_type=prose. The only difference from the declared form is this key, and it is reported as such.
        var mutated = new LinkedHashMap<>(bodies);
        mutated.put("search_metadata_scoped_384",
            withContentTypeOf(bodies.get("search_metadata_scoped_384"), bodies.get(ROUTE_A.get(0))));
        assertThat(disagreements(mutated)).isEmpty();
        assertThat(contentTypeDisagreements(mutated)).hasSize(1)
            .allMatch(s -> s.startsWith("search_metadata_scoped_384 content_type"));
    }

    @Test
    void aKeyRenamedInTheRouteCCopy_isReported() {
        // nexus-u9dc5: the 14th body is held to the same object. A key renamed in manifest_admits_chunk is a drift the
        // route (c) get would answer differently from the stage and from route (b).
        var mutated = withMutated("manifest_admits_chunk", "'extraction_method'", "'extraction_methd'");
        assertThat(disagreements(mutated)).as("a renamed key").hasSize(1)
            .allMatch(s -> s.startsWith("manifest_admits_chunk differs from manifest_search_384"));
    }

    @Test
    void aRouteCContentTypeOnTheOverAdmittingRouteAForm_isReported() {
        // Route (c) has no stage after the read, so the route (a) form that over-admits would return a chunk the stage
        // drops: the only difference from the declared form is this key, and it is reported as such.
        var mutated = new LinkedHashMap<>(bodies);
        mutated.put("manifest_admits_chunk",
            withContentTypeOf(bodies.get("manifest_admits_chunk"), bodies.get(ROUTE_A.get(0))));
        assertThat(disagreements(mutated)).isEmpty();
        assertThat(contentTypeDisagreements(mutated)).hasSize(1)
            .allMatch(s -> s.startsWith("manifest_admits_chunk content_type"));
    }

    @Test
    void aRouteACopyOnTheExactRouteBForm_isReported() {
        var mutated = new LinkedHashMap<>(bodies);
        mutated.put("manifest_search_768",
            withContentTypeOf(bodies.get("manifest_search_768"), bodies.get("search_aspect_scoped_384")));
        assertThat(contentTypeDisagreements(mutated)).hasSize(1)
            .allMatch(s -> s.startsWith("manifest_search_768 content_type"));
    }

    @Test
    void aContentTypeKeyThatMovedBehindTags_failsLoudly() {
        var mutated = withMutated("search_metadata_scoped_384", "'tags', CASE WHEN", "'tagz', CASE WHEN");
        assertThatThrownBy(() -> disagreements(mutated)).isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("no content_type key");
    }

    @Test
    void aChangeInsideTheTagsBranch_isReported() {
        var mutated = withMutated("manifest_search_1024", "position('\"tags\"' in p_manifest_path) > 0",
            "position('\"tags\"' in p_manifest_path) >= 0");
        assertThat(disagreements(mutated)).hasSize(1);
    }

    @Test
    void theCountsTextGate_isTheTextGateOfEveryManifestSearch() {
        assertThat(gateDisagreements(bodies)).as("gates that differ from manifest_search_384's").isEmpty();
        // Non-vacuity: the extracted gate is the whole gate (both arms), not an empty or truncated group.
        String gate = gateClause(bodies.get("manifest_qualifying_hits"));
        assertThat(gate).startsWith("(p_query_text IS NULL").endsWith("|| '%')))")
            .contains("c.chunk_tsv @@ plainto_tsquery('english', p_query_text)")
            .contains("p_query_text OPERATOR(nexus.<%) c.chunk_text")
            .as("vectors-058: the trigram and substring arms read nexus.lexical_trgm")
            .contains("nexus.lexical_trgm").contains("OPERATOR(pg_catalog.~~*)");
    }

    private Map<String, String> withMutatedGate(String proname, String from, String to) {
        return withMutated(proname, from, to);
    }

    @Test
    void aGateArmDroppedFromTheCount_isReported() {
        var mutated = withMutatedGate("manifest_qualifying_hits",
            "AND p_query_text OPERATOR(nexus.<%) c.chunk_text)", "AND false)");
        assertThat(gateDisagreements(mutated)).hasSize(1)
            .allMatch(s -> s.startsWith("manifest_qualifying_hits gate differs from manifest_search_384"));
    }

    @Test
    void aGateChangedInOneManifestSearch_isReportedAgainstTheCountToo() {
        var mutated = withMutatedGate("manifest_search_384", "plainto_tsquery('english', p_query_text)",
            "plainto_tsquery('simple', p_query_text)");
        // The reference moved, so every other copy, the count included, differs from it.
        assertThat(gateDisagreements(mutated)).hasSize(GATED.size() - 1);
    }

    @Test
    void aBodyThatLostItsGate_failsLoudly() {
        var mutated = withMutatedGate("manifest_qualifying_hits", "(p_query_text IS NULL", "(p_query_txt IS NULL");
        assertThatThrownBy(() -> gateDisagreements(mutated)).isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("no text gate");
    }

    @Test
    void layoutAndCommentsAreNotDifferences() {
        var mutated = withMutated("manifest_qualifying_hits", "CASE WHEN m.labels_written",
            "-- a comment\n   CASE   WHEN /* inline */  m.labels_written");
        assertThat(disagreements(mutated)).as("whitespace and comments only").isEmpty();
    }

    @Test
    void aBodyThatLostItsObjectBlock_failsLoudlyInsteadOfComparingEmpties() {
        var mutated = withMutated("manifest_qualifying_hits", ") AS eff", ") AS effective");
        assertThatThrownBy(() -> disagreements(mutated)).isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("no object block end");
    }
}
