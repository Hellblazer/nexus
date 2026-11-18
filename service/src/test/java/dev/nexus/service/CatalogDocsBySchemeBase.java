// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;

import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Set;
import java.util.TreeSet;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-uwggq.1.5 (RDR-241 Phase 2, the fourth engine route): GET /v1/catalog/docs/by-scheme answers a live row iff
 * CPython's urlparse(source_uri).scheme equals the requested scheme lowercased, which is what the client keeps today
 * (doctor_references.is_reference_only). The table below is the matrix: stored source_uris with leading spaces and
 * controls, tabs and CR/LF inside the scheme, a drive path, a digit-led and a space-split scheme, an empty scheme, a
 * non-ASCII letter, and a row per case of capitals. The expected matches are COMPUTED WITH CPython 3.13 urlparse and
 * written into PYTHON_MATCHES. It runs on a C-ctype cluster (CatalogDocsBySchemeCtypeCTest) and on the test cluster's
 * locale (CatalogDocsBySchemeRouteTest); the SQL folds only ASCII and so does not depend on the ctype. A NUL cannot be
 * stored in PostgreSQL text, so no row holds one and the NUL case of urlsplit's strip is not reachable.
 */
abstract class CatalogDocsBySchemeBase extends Rdr241RouteTestBase {

    /** Stored source_uris of tenant A's live rows; index i is tumbler 7.1.(i+1). (A NUL cannot be stored in PostgreSQL text, so no row holds one.) */
    static final String[] URIS = {
        "https://example.org/a",
        "HTTPS://example.org/b",
        " https://example.org/c",
        "\u0009https://example.org/d",
        "\u0001https://example.org/e",
        "ht\u0009tps://example.org/f",
        "ht\u000d\u000atps://example.org/g",
        "C:\\x",
        "1http://x",
        "ht tp://x",
        "https:",
        "http://example.org/h",
        "x-devonthink-item://0A1B2C",
        "nx-note://abc",
        "file:///tmp/a.md",
        "a+b.c-d:x",
        "  \u000a\u0009 HTTPS:x",
        "",
        ":x",
        "https\u00e9://x",
        "\u00e9tt://x",
        "https://example.org/trailing ",
        "\u007fhttps://x",
        "Https://example.org/i",
        "chroma://knowledge__x/doc",
        "\u001f \u000bhttps://example.org/j",
        "https\u000b://x",
    };

    static final String[] SCHEMES = {
        "https",
        "HTTPS",
        "http",
        "c",
        "file",
        "x-devonthink-item",
        "a+b.c-d",
        "nx-note",
        "ht",
        "chroma",
        "Https",
    };

    /** For each requested scheme, the URI indexes with urlparse(uri).scheme == scheme.lower(), computed with CPython 3 urllib.parse.urlparse. */
    static final int[][] PYTHON_MATCHES = {
        {0, 1, 2, 3, 4, 5, 6, 10, 16, 21, 23, 25},
        {0, 1, 2, 3, 4, 5, 6, 10, 16, 21, 23, 25},
        {11},
        {7},
        {14},
        {12},
        {15},
        {13},
        {},
        {24},
        {0, 1, 2, 3, 4, 5, 6, 10, 16, 21, 23, 25},
    };

    static final String PATH = "/v1/catalog/docs/by-scheme";

    static String enc(String s) {
        return URLEncoder.encode(s, StandardCharsets.UTF_8);
    }

    void seedTable() throws Exception {
        Row[] rows = new Row[URIS.length];
        for (int i = 0; i < URIS.length; i++) rows[i] = Row.of("7.1." + (i + 1)).uri(URIS[i]);
        insert(rows);
    }

    /** The URI indexes a scheme selects, paged to completion (the table is far under one page). */
    Set<Integer> engineMatches(String scheme) throws Exception {
        var body = ok(PATH + "?limit=1000&scheme=" + enc(scheme));
        Set<Integer> out = new TreeSet<>();
        for (Object t : tumblers(body)) out.add(Integer.parseInt(((String) t).substring("7.1.".length())) - 1);
        assertThat(body).as("count for " + scheme).containsEntry("count", out.size());
        return out;
    }

    @Test
    void theRouteKeepsExactlyTheRowsUrlparseGivesTheSchemeTo() throws Exception {
        seedTable();
        // the same rows under a deleted flag and in another tenant must never appear
        insert(Row.of("7.2.1").uri("https://example.org/deleted").tombstoned(), Row.of("7.2.2").tenant(TENANT_B).uri("https://example.org/b"));
        assertThat(SCHEMES.length).isEqualTo(PYTHON_MATCHES.length);
        for (int s = 0; s < SCHEMES.length; s++) {
            Set<Integer> python = new TreeSet<>();
            for (int i : PYTHON_MATCHES[s]) python.add(i);
            assertThat(engineMatches(SCHEMES[s])).as("scheme " + SCHEMES[s]).isEqualTo(python);
        }
        // non-vacuity: the table has plenty of rows that are kept and plenty that are not, and the odd forms are among the kept
        assertThat(PYTHON_MATCHES[0].length).as("rows urlparse reads as https").isGreaterThanOrEqualTo(10);
        assertThat(URIS.length).isGreaterThan(20);
        List<Integer> https = java.util.Arrays.stream(PYTHON_MATCHES[0]).boxed().toList();
        assertThat(https).as("a leading space, a leading tab, a leading control, tab and CR/LF inside the scheme, HTTPS, Https")
            .contains(2, 3, 4, 5, 6, 1, 23);
        assertThat(https).as("a drive path, a digit-led scheme, a space inside, a non-ASCII letter, a control inside the scheme are not https")
            .doesNotContain(7, 8, 9, 19, 26);
    }
}
