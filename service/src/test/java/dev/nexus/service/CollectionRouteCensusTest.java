// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
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
 * <p>Both directions are non-vacuous: the examined file and site counts are asserted, and a synthetic source with a new
 * field shows the scan sees it.
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
    private static final Map<String, Map<String, Integer>> PINNED = Map.of(
        "AspectHandler.java", Map.of("collection", 4),
        "CatalogHandler.java", Map.of("collection", 10, "prefer_collection", 1),
        "ChashHandler.java", Map.of("collection", 2),
        "ChunkKeysAdminHandler.java", Map.of("collection", 3),
        "PipelineHandler.java", Map.of("collection", 4),
        "ReembedHandler.java", Map.of("source_collection", 2, "target_collection", 2),
        "RemapHandler.java", Map.of("source_collection", 4, "target_collection", 2),
        "TaxonomyHandler.java", Map.of("collection", 24, "source_collection", 3, "cross_collection", 2,
            "source_collection_prefix", 1, "collections", 1, "collection_name", 1),
        "TelemetryHandler.java", Map.of("collection", 6),
        "VectorHandler.java", Map.of("collection", 14, "collections", 6, "quarantine_collection", 3,
            "origin_collection", 3));

    static String blank(String src) {
        return src.replaceAll("(?s)/\\*.*?\\*/", "").replaceAll("//[^\\n]*", "");
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

        Set<String> accounted = new HashSet<>();
        for (var route : CollectionRouteCensus.ROUTES) {
            for (var slot : route.slots()) {
                accounted.add(route.handler() + "#" + slot.labelField());
            }
        }
        for (var ex : CollectionRouteCensus.EXCLUDED) {
            accounted.add(ex.handler() + "#" + ex.field());
        }
        for (var file : found.entrySet()) {
            for (String field : file.getValue().keySet()) {
                assertThat(accounted).as("%s reads '%s': a census row names it as a slot, or CollectionRouteCensus.EXCLUDED "
                        + "says why the by-id contract leaves it", file.getKey(), field)
                    .contains(file.getKey() + "#" + field);
            }
        }
    }

    @Test
    void everyRowIsDispatchedByItsHandlerAndItsMethodExists() throws Exception {
        assertThat(CollectionRouteCensus.ROUTES).as("non-vacuity").hasSizeGreaterThanOrEqualTo(80);
        Set<String> seen = new HashSet<>();
        for (var route : CollectionRouteCensus.ROUTES) {
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
