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
 * <p>Besides the total, three labelled views name who and what, so Step 2b can read the writers and keys from the
 * status route without the log: by route and key class, by offending key, and by client ({@code User-Agent} and
 * {@code X-Nexus-Client-Version}). Each view counts a request once per label it carries (a request that names three
 * document keys adds one to its route and document class, and one to each of the three keys). The views are
 * bounded, so a client cannot grow them: routes and classes are fixed sets; at most {@link #TRACK_KEYS} distinct
 * keys and {@link #TRACK_CLIENTS} distinct clients are remembered, and what does not fit goes to an overflow count
 * of its own. A key or client text is client-supplied and arrives cleaned by the check.
 *
 * <p>JVM-wide, never keyed by tenant, and reset only by tests; a tenant reset does not touch it. Step 2a is log mode
 * only, so nothing counts a refusal here; Step 2c adds that field when it adds the 422.
 */
public final class ChunkKeyAllowlistActivity {

    /** Distinct offending keys remembered; a key past this count adds to {@link #keyOverflow()}. */
    static final int TRACK_KEYS = 256;
    /** Distinct clients remembered; a client past this count adds to {@link #clientOverflow()}. */
    static final int TRACK_CLIENTS = 64;
    /** Keys the status route names, most frequent first; the rest, tracked or not, are in the overflow. */
    public static final int TOP_KEYS = 32;

    private static final AtomicLong WOULD_REFUSE = new AtomicLong();
    private static final ConcurrentHashMap<String, LongAdder> BY_ROUTE_CLASS = new ConcurrentHashMap<>();
    private static final ConcurrentHashMap<String, LongAdder> BY_KEY = new ConcurrentHashMap<>();
    private static final ConcurrentHashMap<String, LongAdder> BY_CLIENT = new ConcurrentHashMap<>();
    private static final AtomicLong KEY_OVERFLOW = new AtomicLong();
    private static final AtomicLong CLIENT_OVERFLOW = new AtomicLong();

    private ChunkKeyAllowlistActivity() {
    }

    /** Requests of one route that named at least one key of one class. */
    public record RouteClassCount(String route, String keyClass, long count) { }

    /** Requests that named one key, filed under that key's class. */
    public record KeyCount(String key, String keyClass, long count) { }

    /** Requests of one client. */
    public record ClientCount(String userAgent, String clientVersion, long count) { }

    /**
     * Count one offending request.
     *
     * @param route     the route constant the handler passed
     * @param offending the cleaned key text of each distinct offending key, with its class
     * @param userAgent the cleaned {@code User-Agent}, never null
     * @param version   the cleaned {@code X-Nexus-Client-Version}, never null
     */
    static void recordWouldRefuse(String route, Map<String, ChunkKeyAllowlistCheck.KeyClass> offending,
                                  String userAgent, String version) {
        WOULD_REFUSE.incrementAndGet();
        offending.values().stream().distinct().forEach(c ->
            BY_ROUTE_CLASS.computeIfAbsent(route + "|" + c.wire(), k -> new LongAdder()).increment());
        for (var e : offending.entrySet()) {
            if (!bump(BY_KEY, e.getValue().wire() + ":" + e.getKey(), TRACK_KEYS)) {
                KEY_OVERFLOW.incrementAndGet();
            }
        }
        if (!bump(BY_CLIENT, userAgent + "\u0001" + version, TRACK_CLIENTS)) {
            CLIENT_OVERFLOW.incrementAndGet();
        }
    }

    /** Adds one to {@code key}; false when the key is new and the map is at its cap. */
    private static boolean bump(ConcurrentHashMap<String, LongAdder> map, String key, int cap) {
        LongAdder adder = map.get(key);
        if (adder == null) {
            synchronized (map) {
                adder = map.get(key);
                if (adder == null) {
                    if (map.size() >= cap) {
                        return false;
                    }
                    adder = new LongAdder();
                    map.put(key, adder);
                }
            }
        }
        adder.increment();
        return true;
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

    /** @return every remembered key, most frequent first, then by class and name. */
    private static List<KeyCount> allKeys() {
        List<KeyCount> out = new ArrayList<>();
        BY_KEY.forEach((k, v) -> {
            int colon = k.indexOf(':');
            out.add(new KeyCount(k.substring(colon + 1), k.substring(0, colon), v.sum()));
        });
        out.sort(Comparator.comparingLong(KeyCount::count).reversed()
            .thenComparing(KeyCount::keyClass).thenComparing(KeyCount::key));
        return out;
    }

    /** @return the {@link #TOP_KEYS} most frequent offending keys, most frequent first. */
    public static List<KeyCount> topKeys() {
        List<KeyCount> all = allKeys();
        return List.copyOf(all.subList(0, Math.min(TOP_KEYS, all.size())));
    }

    /** @return key occurrences not named by {@link #topKeys()}: the remembered keys below the cut, and the unremembered. */
    public static long keyOverflow() {
        List<KeyCount> all = allKeys();
        long below = 0;
        for (int i = Math.min(TOP_KEYS, all.size()); i < all.size(); i++) {
            below += all.get(i).count();
        }
        return below + KEY_OVERFLOW.get();
    }

    /** @return every remembered client, most frequent first. */
    public static List<ClientCount> clients() {
        List<ClientCount> out = new ArrayList<>();
        BY_CLIENT.forEach((k, v) -> {
            int sep = k.indexOf('\u0001');
            out.add(new ClientCount(k.substring(0, sep), k.substring(sep + 1), v.sum()));
        });
        out.sort(Comparator.comparingLong(ClientCount::count).reversed()
            .thenComparing(ClientCount::userAgent).thenComparing(ClientCount::clientVersion));
        return out;
    }

    /** @return requests from clients past the remembered set. */
    public static long clientOverflow() {
        return CLIENT_OVERFLOW.get();
    }

    /** Test-only reset: production code never calls this. */
    public static void resetForTests() {
        WOULD_REFUSE.set(0);
        BY_ROUTE_CLASS.clear();
        BY_KEY.clear();
        BY_CLIENT.clear();
        KEY_OVERFLOW.set(0);
        CLIENT_OVERFLOW.set(0);
    }
}
