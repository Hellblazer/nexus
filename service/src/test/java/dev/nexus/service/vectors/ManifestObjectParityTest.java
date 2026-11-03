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
 * <p>TO COVER ANOTHER FUNCTION add its name to {@link #FUNCTIONS}. The nine route (b) bodies of nexus-6pfgb.41 join
 * there once they carry the same object. The extractor ({@link #objectBlock}) and the comparison ({@link
 * #disagreements}) are static and take plain strings, so they can be pointed at any source text.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ManifestObjectParityTest {

    /** The functions that carry the per-occurrence object. The first is the reference the others are held to. */
    static final List<String> FUNCTIONS = List.of(
        "manifest_search_384", "manifest_search_768", "manifest_search_1024", "manifest_qualifying_hits");

    /** The keys the object builds; a block that names fewer than this has been cut in the wrong place. */
    private static final int MIN_NAMED_KEYS = 17;

    private static final Pattern LINE_COMMENT = Pattern.compile("--[^\n]*");
    private static final Pattern BLOCK_COMMENT = Pattern.compile("/\\*.*?\\*/", Pattern.DOTALL);
    private static final Pattern WHITESPACE = Pattern.compile("\\s+");
    private static final Pattern BLOCK_START =
        Pattern.compile("CROSS\\s+JOIN\\s+LATERAL\\s*\\(\\s*SELECT\\s+nexus\\.catalog_source_format");
    private static final Pattern BLOCK_END = Pattern.compile("\\)\\s+AS\\s+eff\\s*\\)\\s+e\\b");

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

    /** The names (other than the first, the reference) whose block differs from the first's. */
    static List<String> disagreements(Map<String, String> bodiesByName) {
        List<String> out = new ArrayList<>();
        String referenceName = null;
        String reference = null;
        for (var e : bodiesByName.entrySet()) {
            String block = objectBlock(e.getValue());
            if (reference == null) {
                referenceName = e.getKey();
                reference = block;
            } else if (!reference.equals(block)) {
                out.add(e.getKey() + " differs from " + referenceName + " at " + firstDifference(reference, block));
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
    void everyFunctionBuildsTheSameObject() {
        assertThat(disagreements(bodies))
            .as("functions whose per-occurrence object differs from %s", FUNCTIONS.get(0)).isEmpty();
    }

    @Test
    void theExtractedBlockIsTheObject_notAnEmptyOrTruncatedRegion() {
        // Non-vacuity: equal-and-empty would pass the comparison, so each block must be the whole object.
        for (var e : bodies.entrySet()) {
            String block = objectBlock(e.getValue());
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
    void aChangeInsideTheTagsBranch_isReported() {
        var mutated = withMutated("manifest_search_1024", "position('\"tags\"' in p_manifest_path) > 0",
            "position('\"tags\"' in p_manifest_path) >= 0");
        assertThat(disagreements(mutated)).hasSize(1);
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
