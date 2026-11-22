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

    /**
     * The embed and re-embed routes answer 503 before they read their collection on the standard engine (no embedder, not
     * local), so a selector check can only reach them on the LOCAL_EMBED variant. Exactly these three rows use it; every other
     * row runs on the standard engine, whose mode is unchanged.
     */
    @Test
    void exactlyTheEmbedAndReembedRowsRunOnTheLocalEmbedEngine() {
        Set<String> local = new TreeSet<>();
        for (var route : CollectionRouteCensus.ROUTES) {
            if (route.engine() == CollectionRouteCensus.Engine.LOCAL_EMBED) {
                local.add(route.label());
            }
        }
        assertThat(local).containsExactly("POST /v1/vectors/embed", "POST /v1/vectors/reembed/jobs",
            "POST /v1/vectors/reembed/plan");
    }

    /**
     * A selector can sit inside each element of a request array (the centroid upsert reads "collection" from every element of
     * "records"). The census names that array, the row has one slot, the id twin keeps the convention's spelling, and the
     * handler source really names the array. Exactly one row is nested.
     */
    @Test
    void theNestedSelectorRowIsTheCentroidUpsertAndItsHandlerReadsTheArray() throws Exception {
        Set<String> nested = new TreeSet<>();
        for (var route : CollectionRouteCensus.ROUTES) {
            for (var slot : route.slots()) {
                if (slot.nested()) {
                    nested.add(route.label());
                    assertThat(route.slots()).as("%s: a nested row has exactly one slot", route.label()).hasSize(1);
                    assertThat(slot.idField()).isEqualTo(expectedIdField(slot));
                    String src = blank(Files.readString(HTTP_DIR.resolve(route.handler())));
                    assertThat(src).as("%s: %s names the array '%s'", route.label(), route.handler(), slot.nestedIn())
                        .contains("\"" + slot.nestedIn() + "\"");
                }
            }
        }
        assertThat(nested).containsExactly("POST /v1/taxonomy/centroids/upsert");
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
        for (var route : CollectionRouteCensus.ROUTES) {
            for (var slot : route.slots()) {
                assertThat(slot.idField()).as("%s: the id twin of '%s'", route.label(), slot.labelField())
                    .isEqualTo(expectedIdField(slot));
            }
        }
        Set<String> allowed = new HashSet<>(CollectionRouteCensus.ID_NAMES_OTHER_THAN_SLOTS);
        for (var route : CollectionRouteCensus.ROUTES) {
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
