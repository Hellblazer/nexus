// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

/**
 * The query vector a search already computed, with the one collection it may enter the query ring of (RDR-230
 * Technical Design 6, bead nexus-4lnn1.1.3). The repository decides {@code collection} while it still holds the
 * per-arm figures; the handler passes the value to {@link PciQueryRing#admit} after the response is sent. Nothing is
 * embedded for the ring.
 *
 * <p>The rule: a query vector enters collection X's ring when the request searched X alone, or when X's arm returned
 * the nearest row among the request's arms (every arm of one request shares one embedding model, so "among the arms
 * that share X's model" is "among the arms"). Admitting every arm of a fan-out would turn a far query's ordinary
 * approximation error on a correct index into a red canary (RDR-227 measured 0.975 to 0.983 on one).
 *
 * @param model      the embedding model of the request's collections
 * @param vector     the query embedding; treated as frozen
 * @param collection the collection to admit to, or {@code null} when no arm qualifies (a fan-out in which no arm
 *                   returned a row, or whose only arm failed)
 */
public record QueryVector(String model, float[] vector, String collection) {

    /** The vector of a request that named exactly one collection (after unregistered names were dropped). */
    public static QueryVector alone(String model, float[] vector, String collection) {
        return new QueryVector(model, vector, collection);
    }

    /**
     * The vector of a request that searched several collections: admitted to the collection that returned the nearest
     * row, {@code null} when none returned one. A tie goes to the first.
     *
     * @param collections the arms, in request order
     * @param distances   each arm's smallest raw distance, aligned with {@code collections}; {@code null} for an
     *                    arm that returned no row or failed
     */
    public static QueryVector nearestOf(String model, float[] vector, java.util.List<String> collections,
                                        java.util.List<Double> distances) {
        String nearest = null;
        Double best = null;
        for (int i = 0; i < collections.size(); i++) {
            Double d = distances.get(i);
            if (d != null && (best == null || d < best)) {
                best = d;
                nearest = collections.get(i);
            }
        }
        return new QueryVector(model, vector, nearest);
    }
}
