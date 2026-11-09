// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.LongAdder;

/**
 * RDR-236 Phase 4 Step 2a (nexus-6pfgb.66): the process-wide, since-boot count of client chunk-metadata writes
 * that carried a key outside the allow-list. One count per REQUEST, however many keys or chunks offended, and
 * counted whether or not the log line was rate limited: the counters on {@code GET /v1/status} are the complete
 * signal, the log line a sample.
 *
 * <p>Besides the total there is one view, by route and key class. Both labels are fixed by the engine (the route
 * constants the handlers pass, and the four key classes document, position, legacy and other), so the view is
 * bounded by construction and a client cannot grow it. A request that names keys of two classes adds one to each
 * class.
 *
 * <p>There is deliberately no view by key name or by client. {@code GET /v1/status} has no tenant scope: any caller
 * sees the whole engine's status, so a key name, a {@code User-Agent} or a client version, all supplied by one
 * tenant's client, would be shown to every other tenant. That detail is in the {@code chunk_metadata_key_would_refuse}
 * log line instead, which is rate limited per route, tenant, collection, key set and client, so it names every
 * distinct writer while the limiter has room. Past its 10,000 keys, new writers share one overflow line, so the log
 * is a sample, not a proof that no writer was missed.
 *
 * <p>JVM-wide, never keyed by tenant, and reset only by tests; a tenant reset does not touch it. Step 2a is log mode
 * only, so nothing counts a refusal here; Step 2c adds that field when it adds the 422.
 */
public final class ChunkKeyAllowlistActivity {

    private static final AtomicLong WOULD_REFUSE = new AtomicLong();
    private static final ConcurrentHashMap<String, LongAdder> BY_ROUTE_CLASS = new ConcurrentHashMap<>();

    private ChunkKeyAllowlistActivity() {
    }

    /** Requests of one route that named at least one key of one class. */
    public record RouteClassCount(String route, String keyClass, long count) { }

    /**
     * Count one offending request.
     *
     * @param route     the route constant the handler passed
     * @param offending the distinct offending keys with their class; only the classes are recorded
     */
    static void recordWouldRefuse(String route, Map<String, ChunkKeyAllowlistCheck.KeyClass> offending) {
        WOULD_REFUSE.incrementAndGet();
        offending.values().stream().distinct().forEach(c ->
            BY_ROUTE_CLASS.computeIfAbsent(route + "|" + c.wire(), k -> new LongAdder()).increment());
    }

    /** @return client chunk-metadata writes that named a key outside the allow-list since boot. */
    public static long wouldRefuseTotal() {
        return WOULD_REFUSE.get();
    }

    /** @return the by-route-and-class counts, sorted by route then class; empty when nothing offended. */
    public static List<RouteClassCount> byRouteClass() {
        List<RouteClassCount> out = new ArrayList<>();
        BY_ROUTE_CLASS.forEach((k, v) -> {
            int bar = k.indexOf('|');
            out.add(new RouteClassCount(k.substring(0, bar), k.substring(bar + 1), v.sum()));
        });
        out.sort(Comparator.comparing(RouteClassCount::route).thenComparing(RouteClassCount::keyClass));
        return out;
    }

    /** Test-only reset: production code never calls this. */
    public static void resetForTests() {
        WOULD_REFUSE.set(0);
        BY_ROUTE_CLASS.clear();
    }
}
