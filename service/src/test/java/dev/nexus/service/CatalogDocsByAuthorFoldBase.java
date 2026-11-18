// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;

import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.TreeSet;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-uwggq.2.3 (RDR-241 Phase 2, engine review r1 ship-blocker): the author route folds case with the pg_c_utf8
 * collation's simple Unicode case mapping, whatever the database's own ctype, because the local bundle is
 * initdb --no-locale (C ctype), where a bare lower() folds ASCII only and would leave a capital O with diaeresis out of
 * a match for the small one. The same matrix runs on both ctypes (CatalogDocsByAuthorFoldCtypeCTest on a cluster
 * initdb'd as the bundle is, CatalogDocsByAuthorFoldDefaultCtypeTest on the test cluster's locale), so the answers are
 * shown independent of the cluster.
 *
 * <p>The oracle is Python's needle.lower() in author.lower() (the MCP _keep predicate), with the expected matches
 * COMPUTED WITH CPython 3 and written into PYTHON_MATCHES. The engine uses Unicode SIMPLE case mapping (one code point to
 * one), Python the FULL mapping, and they differ in exactly the pairs of DIVERGENCES: U+0130 (capital I with dot above)
 * lowers to "i" plus U+0307 in Python and to "i" in the engine, and a capital sigma lowers to a final sigma at the end of
 * a word in Python and always to the medial sigma in the engine. The test asserts that the engine's answers equal
 * Python's everywhere except those pairs, and that the pairs it finds are exactly the listed ones, so a new difference
 * and a silently closed one both fail. The listed pairs go to bead nexus-uwggq.2.6; none is excused here.
 */
abstract class CatalogDocsByAuthorFoldBase extends Rdr241RouteTestBase {

    /** Authors of the fixture; index i is tumbler 8.1.(i+1). The last two are an empty and a null author. */
    static final String[] AUTHORS = {
        "Leslie Lamport",
        "G\u00f6del, Kurt",
        "Go\u0308del, Kurt",
        "G\u00d6DEL",
        "Paul Erd\u0151s",
        "Anders \u00c5ngstr\u00f6m",
        "Jan \u0141ukasiewicz",
        "Stra\u00dfe",
        "\u0130smet \u0130n\u00f6n\u00fc",
        "\u03a3\u03af\u03c3\u03c5\u03c6\u03bf\u03c2",
        "\u041f\u0443\u0448\u043a\u0438\u043d",
        "\u6e6f\u5ddd\u79c0\u6a39",
        "100%_done",
        "",
        "M\u00dcLLER",
        "\u03a3\u038a\u03a3\u03a5\u03a6\u039f\u03a3",
        "STRA\u1e9eE",
        "",
        null
    };

    static final String[] NEEDLES = {
        "lamport",
        "hild",
        "g\u00f6del",
        "G\u00d6DEL",
        "\u00f6del",
        "erd\u0151s",
        "ERD\u0150S",
        "\u00e5ngstr\u00f6m",
        "\u0142ukasiewicz",
        "stra\u00dfe",
        "\u0130n\u00f6n\u00fc",
        "in\u00f6n\u00fc",
        "\u03c3\u03af\u03c3\u03c5\u03c6\u03bf\u03c2",
        "\u041f\u0423\u0428\u041a\u0418\u041d",
        "\u6e6f\u5ddd",
        "%",
        "_",
        "100%_",
        "\u00fc",
        "\u00dc",
        "m\u00fcller",
        "\u03a3\u038a\u03a3\u03a5\u03a6\u039f\u03a3",
        "\u0130",
        "i",
        "STRA\u1e9eE",
        "\u1e9e",
    };

    /** For each needle, the author indexes for which Python's {@code needle.lower() in author.lower()} is true (computed with CPython str.lower()). */
    static final int[][] PYTHON_MATCHES = {
        {0},   // needle #0
        {},   // needle #1
        {1, 3},   // needle #2
        {1, 3},   // needle #3
        {1, 3},   // needle #4
        {4},   // needle #5
        {4},   // needle #6
        {5},   // needle #7
        {6},   // needle #8
        {7, 16},   // needle #9
        {8},   // needle #10
        {},   // needle #11
        {9, 15},   // needle #12
        {10},   // needle #13
        {11},   // needle #14
        {12},   // needle #15
        {12},   // needle #16
        {12},   // needle #17
        {8, 14},   // needle #18
        {8, 14},   // needle #19
        {14},   // needle #20
        {9, 15},   // needle #21
        {8},   // needle #22
        {0, 6, 8},   // needle #23
        {7, 16},   // needle #24
        {7, 16},   // needle #25
    };

    /** {needle index, author index, what the engine answers}: the pairs where pg_c_utf8 (simple case mapping) differs from CPython str.lower() (full mapping). */
    static final int[][] DIVERGENCES = {
        {11, 8, 1},   // needle 'in\xf6n\xfc' vs author '\u0130smet \u0130n\xf6n\xfc': str.lower() says False, the engine says True
        {12, 15, 0},   // needle '\u03c3\u03af\u03c3\u03c5\u03c6\u03bf\u03c2' vs author '\u03a3\u038a\u03a3\u03a5\u03a6\u039f\u03a3': str.lower() says True, the engine says False
        {21, 9, 0},   // needle '\u03a3\u038a\u03a3\u03a5\u03a6\u039f\u03a3' vs author '\u03a3\u03af\u03c3\u03c5\u03c6\u03bf\u03c2': str.lower() says True, the engine says False
        {22, 0, 1},   // needle '\u0130' vs author 'Leslie Lamport': str.lower() says False, the engine says True
        {22, 6, 1},   // needle '\u0130' vs author 'Jan \u0141ukasiewicz': str.lower() says False, the engine says True
    };

    private static String enc(String s) {
        return URLEncoder.encode(s, StandardCharsets.UTF_8);
    }

    private void seedAuthors() throws Exception {
        Row[] rows = new Row[AUTHORS.length];
        for (int i = 0; i < AUTHORS.length; i++) rows[i] = Row.of("8.1." + (i + 1)).author(AUTHORS[i]);
        insert(rows);
    }

    /** The author indexes a needle selects (a single page here: the fixture is far under 1,000). */
    private Set<Integer> engineMatches(String needle) throws Exception {
        var body = ok("/v1/catalog/docs/by-author?limit=1000&author=" + enc(needle));
        Set<Integer> out = new TreeSet<>();
        for (Object t : tumblers(body)) out.add(Integer.parseInt(((String) t).substring("8.1.".length())) - 1);
        assertThat(body).as("count for " + needle).containsEntry("count", out.size());
        return out;
    }

    @Test
    void theEngineFoldsLikePythonExceptWhereSimpleAndFullCaseMappingDiffer() throws Exception {
        seedAuthors();
        assertThat(NEEDLES.length).isEqualTo(PYTHON_MATCHES.length);
        List<String> found = new ArrayList<>();
        List<String> listed = new ArrayList<>();
        for (int[] d : DIVERGENCES) listed.add(d[0] + ":" + d[1] + ":" + d[2]);
        for (int n = 0; n < NEEDLES.length; n++) {
            Set<Integer> python = new TreeSet<>();
            for (int a : PYTHON_MATCHES[n]) python.add(a);
            Set<Integer> engine = engineMatches(NEEDLES[n]);
            Set<Integer> universe = new TreeSet<>(python);
            universe.addAll(engine);
            for (int a : universe) {
                boolean py = python.contains(a);
                boolean en = engine.contains(a);
                if (py != en) found.add(n + ":" + a + ":" + (en ? 1 : 0));
            }
        }
        assertThat(found).as("(needle, author, engine answer) triples where the engine differs from str.lower(); see DIVERGENCES")
            .containsExactlyInAnyOrderElementsOf(listed);
        // non-vacuity: a real matrix, most needles select rows, and the divergences are a small minority
        assertThat(NEEDLES.length * AUTHORS.length).isGreaterThan(400);
        long selecting = java.util.Arrays.stream(PYTHON_MATCHES).filter(m -> m.length > 0).count();
        assertThat(selecting).as("needles that select at least one author").isGreaterThanOrEqualTo(20);
        assertThat(DIVERGENCES.length).isLessThan(10);
    }

    @Test
    void aCapitalUWithDiaeresisFoldsToTheSmallOne_whichAnAsciiOnlyLowerWouldNotDo() throws Exception {
        insert(Row.of("8.2.1").author("MÜLLER"), Row.of("8.2.2").author("Müller"), Row.of("8.2.3").author("MULLER"));
        assertThat(tumblers(ok("/v1/catalog/docs/by-author?author=" + enc("müller")))).containsExactly("8.2.1", "8.2.2");
        assertThat(tumblers(ok("/v1/catalog/docs/by-author?author=" + enc("MÜLLER")))).containsExactly("8.2.1", "8.2.2");
        assertThat(tumblers(ok("/v1/catalog/docs/by-author?author=" + enc("ü")))).containsExactly("8.2.1", "8.2.2");
        assertThat(tumblers(ok("/v1/catalog/docs/by-author?author=muller")))
            .as("a plain u is not an u with diaeresis").containsExactly("8.2.3");
    }
}
