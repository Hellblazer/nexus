// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.TaxonomyCoverage;

import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;

/**
 * RDR-134 Step 3 (bead nexus-7fku2.1.3): the {@code topic_route} parameter of {@code POST
 * /v1/vectors/search-per-collection}, as the repository reads it.
 *
 * <p>Wire form: {@code {"mode": "top1" | "band", "width": <similarity difference, band only, >= 0>, "min_coverage":
 * <0..1>, "collections": [...]}}. {@code min_coverage} is optional (default {@link #DEFAULT_MIN_COVERAGE}, 0.9): the
 * coverage a collection needs before it is routed, so a threshold under study is a request value and not an engine
 * release. A number outside 0..1, or a value that is not a number, is a 400. {@code collections} is the prose-only carrier (a departure from the RDR's request-wide field, see the bead):
 * only the collections named are routed, and every other collection of the request is searched unscoped with status
 * {@code skipped}. It is needed because on a local bge install code and docs collections share one model group, so a
 * request-wide field could not keep code unrouted. The names must also appear in the request's {@code collections}.
 *
 * <ul>
 *   <li>{@code collections} absent (or JSON null): every collection of the request is routed, the RDR's original
 *       meaning.</li>
 *   <li>{@code collections} present and empty: nothing is routed and the response is byte-identical to a request
 *       without {@code topic_route} (no status on any {@code per_collection} entry).</li>
 * </ul>
 *
 * <p><b>Code collections are never routed.</b> A collection whose registered content type is {@code code} is searched
 * unscoped and reports {@code skipped}, whether {@code collections} is absent or names it. The guard is the
 * repository's ({@code PgVectorRepository}, by the registry's content type: the engine does not parse collection
 * names), decided as "skipped" and not as a 400 because the registry is read per tenant and parsing has none. Code
 * collections can have centroids, so the guard is the engine's, not an accident of the data; the client never sends
 * a code name.
 *
 * @param mode           which topics are kept
 * @param width          {@code band} only: a topic is kept when its centroid similarity is within {@code width} of the
 *                       nearest centroid's; 0 for {@code top1}
 * @param collections    the collections to route, {@code null} for all of the request's; empty routes none
 * @param coverage       the coverage reading a collection is gated on ({@link TaxonomyCoverage#lookup} in production)
 * @param requestSkipped true when the request carries {@code chash_allow} or {@code where}: no collection is routed
 *                       and every one reports {@code skipped} (the repository sets it, see {@link #skipRequest()})
 * @param minCoverage    the coverage ({@code 0..1}) a collection needs to be routed
 *                       ({@link TaxonomyCoverage.Counts#routable(double)}); echoed on the response as
 *                       {@code topic_route_min_coverage}
 */
public record TopicRoute(Mode mode, double width, Set<String> collections, CoverageLookup coverage,
                         boolean requestSkipped, double minCoverage) {

    /** The coverage a collection needs when the request names none. */
    public static final double DEFAULT_MIN_COVERAGE = TaxonomyCoverage.ROUTE_MIN_COVERAGE;

    /** Which topics the centroid ranking keeps. */
    public enum Mode {
        /** The nearest topic only. */
        TOP1("top1"),
        /** Every topic whose centroid similarity is within {@code width} of the nearest one's. */
        BAND("band");

        private final String wire;

        Mode(String wire) {
            this.wire = wire;
        }

        public String wire() {
            return wire;
        }
    }

    /**
     * What a {@code per_collection} entry reports about the route. Wire names are a stable contract.
     * {@code no_topics}: the engine could not establish a usable topic set (no centroids, no chunks, coverage below
     * the route's {@code min_coverage}, 0.9 unless the request says otherwise, the coverage reading or the centroid ranking unavailable). {@code fallback}: a topic set was established and
     * the routed search did not deliver rows (zero rows, or the search failed). Both searched the collection unscoped.
     * A collection whose arm ended with an error reports no status at all (its {@code error} says why), and a failed
     * routing stage carries a short {@code topic_route_error} beside the status.
     */
    public enum Status {
        ROUTED("routed"), NO_TOPICS("no_topics"), FALLBACK("fallback"), SKIPPED("skipped");

        private final String wire;

        Status(String wire) {
            this.wire = wire;
        }

        public String wire() {
            return wire;
        }
    }

    /**
     * The coverage reading of one collection; empty when it could not be taken (never a failure of the request).
     * {@code maxStatementMs} is the longest a count statement run for this call may take (the routed arm's share of
     * its remaining budget, {@link TopicRouteArm}); a cached reading ignores it, and {@code 0} means the lookup's own
     * default bound.
     */
    @FunctionalInterface
    public interface CoverageLookup {
        Optional<TaxonomyCoverage.Counts> lookup(String tenant, String collection, int maxStatementMs);
    }

    /** A route at the default minimum coverage. */
    public TopicRoute(Mode mode, double width, Set<String> collections, CoverageLookup coverage,
                      boolean requestSkipped) {
        this(mode, width, collections, coverage, requestSkipped, DEFAULT_MIN_COVERAGE);
    }

    public TopicRoute {
        if (mode == null) {
            throw new IllegalArgumentException("topic_route mode is required");
        }
        if (!(minCoverage >= 0.0 && minCoverage <= 1.0)) {
            throw new IllegalArgumentException("topic_route min_coverage must be a number from 0 to 1, got " + minCoverage);
        }
        if (mode == Mode.BAND && !(width >= 0.0)) {
            throw new IllegalArgumentException("topic_route width must be >= 0 for band, got " + width);
        }
        collections = collections == null ? null : Set.copyOf(collections);
    }

    /**
     * Parse the request's {@code topic_route} value. Null (absent or JSON null) is {@code null}: no route.
     *
     * @throws IllegalArgumentException (a 400) for a value that is not an object, a missing or unknown {@code mode}, a
     *         {@code band} without a numeric {@code width} {@code >= 0}, a {@code min_coverage} that is not a number from
     *         0 to 1 (JSON null is absent: the default), a {@code collections} that is not an array of strings, or a
     *         name not in {@code requestCollections}
     */
    public static TopicRoute parse(Object raw, List<String> requestCollections, CoverageLookup coverage) {
        if (raw == null) {
            return null;
        }
        if (!(raw instanceof Map<?, ?> m)) {
            throw new IllegalArgumentException("field 'topic_route' must be an object");
        }
        Mode mode = null;
        if (m.get("mode") instanceof String s) {
            for (Mode candidate : Mode.values()) {
                if (candidate.wire.equals(s)) {
                    mode = candidate;
                }
            }
        }
        if (mode == null) {
            throw new IllegalArgumentException("topic_route.mode must be \"top1\" or \"band\", got " + m.get("mode"));
        }
        double width = 0.0;
        if (mode == Mode.BAND) {
            if (!(m.get("width") instanceof Number n)) {
                throw new IllegalArgumentException("topic_route.width is required for mode \"band\" and must be a number");
            }
            width = n.doubleValue();
            if (!(width >= 0.0) || Double.isInfinite(width)) {
                throw new IllegalArgumentException("topic_route.width must be a finite number >= 0, got " + n);
            }
        }
        double minCoverage = DEFAULT_MIN_COVERAGE;
        Object rawMin = m.get("min_coverage");
        if (rawMin != null) {
            if (!(rawMin instanceof Number n)) {
                throw new IllegalArgumentException("topic_route.min_coverage must be a number from 0 to 1, got " + rawMin);
            }
            minCoverage = n.doubleValue();
            if (!(minCoverage >= 0.0 && minCoverage <= 1.0)) {
                throw new IllegalArgumentException("topic_route.min_coverage must be a number from 0 to 1, got " + n);
            }
        }
        Set<String> collections = null;
        Object listed = m.get("collections");
        if (listed != null) {
            if (!(listed instanceof List<?> list)) {
                throw new IllegalArgumentException("topic_route.collections must be an array of collection names");
            }
            collections = new LinkedHashSet<>();
            for (Object item : list) {
                if (!(item instanceof String name) || name.isBlank()) {
                    throw new IllegalArgumentException("topic_route.collections must hold non-blank strings");
                }
                if (!requestCollections.contains(name)) {
                    throw new IllegalArgumentException(
                        "topic_route.collections names '" + name + "', which is not in collections");
                }
                collections.add(name);
            }
        }
        return new TopicRoute(mode, width, collections, coverage, false, minCoverage);
    }

    /** True when {@code per_collection} entries carry a status: false for a present but empty {@code collections}. */
    public boolean reportsStatus() {
        return collections == null || !collections.isEmpty();
    }

    /**
     * True when {@code collection} is routed by THIS route: the whole request is not skipped, and the list names it or
     * is absent. A code collection is excluded by the repository, which knows the tenant's registry.
     */
    public boolean routes(String collection) {
        return !requestSkipped && (collections == null || collections.contains(collection));
    }

    /** This route for a request that carries {@code chash_allow} or {@code where}: nothing is routed. */
    public TopicRoute skipRequest() {
        return requestSkipped ? this : new TopicRoute(mode, width, collections, coverage, true, minCoverage);
    }
}
