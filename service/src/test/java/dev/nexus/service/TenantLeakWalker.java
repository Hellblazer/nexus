// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import java.lang.reflect.Field;
import java.lang.reflect.Modifier;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Collections;
import java.util.Deque;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.TreeMap;
import java.util.concurrent.atomic.AtomicReference;

/**
 * Bead nexus-vr8wk (round-1 critic S4 of nexus-0bl77): the behavioural half of the tenant-keyed state check.
 * {@code TenantKeyedStateGateTest} proves every holder was ACKNOWLEDGED as keyed or not; this walks the live
 * object graph and says whether a tenant's name is still reachable. A holder the table wrongly calls "not keyed"
 * passes the gate and is found here.
 *
 * <p>What it walks: the given roots, and the static fields of the given classes, by reflection. It descends into
 * {@code dev.nexus.*} objects (every instance field, superclass chain included, lambda captures too), into
 * {@link Map} keys and values, {@link Collection} elements, reference arrays, {@link Optional} and
 * {@link AtomicReference}. It reads a String, never anything else, for the needle, and reports the path to each
 * hit.
 *
 * <p>What it does NOT see, stated so a green walk is read for what it covers: any other JDK or third-party object
 * (an executor's queue, the HTTP server's contexts, a connection pool) is opaque and counted by class in
 * {@link Result#opaque()}; a tenant referenced only by a hash of its name; and a holder nothing reachable from the
 * roots or the listed classes refers to.
 */
final class TenantLeakWalker {

    /** One reachable string that contains the needle: where it was found and the string itself (cut at 120 chars). */
    record Hit(String path, String value) {
        @Override
        public String toString() {
            return path + " = \"" + value + "\"";
        }
    }

    /**
     * @param hits           every string containing the needle (capped at {@link #MAX_HITS})
     * @param objects        distinct objects visited
     * @param staticOwners   classes whose static fields were read
     * @param staticSkipped  classes named as static owners that could not be read (load or init failure)
     * @param opaque         classes the walk reached but does not descend into, with how many instances
     */
    record Result(List<Hit> hits, int objects, int staticOwners, List<String> staticSkipped,
                  Map<String, Integer> opaque) {
        /** The distinct leading path segments of the hits, e.g. {@code TokenCache.map}: which structures hold it. */
        java.util.Set<String> holders() {
            var out = new java.util.TreeSet<String>();
            for (Hit h : hits) {
                out.add(h.path());
            }
            return out;
        }
    }

    static final int MAX_HITS = 500;
    private static final int MAX_OBJECTS = 3_000_000;
    private static final int MAX_DEPTH = 64;

    private record Node(String path, Object value, int depth) { }

    private final String needle;
    private final Set<Object> seen = Collections.newSetFromMap(new IdentityHashMap<>());
    private final Set<Class<?>> staticsRead = new java.util.HashSet<>();
    private final Deque<Node> todo = new ArrayDeque<>();
    private final List<Hit> hits = new ArrayList<>();
    private final Map<String, Integer> opaque = new TreeMap<>();
    private final List<String> staticSkipped = new ArrayList<>();
    private int objects;

    private TenantLeakWalker(String needle) {
        this.needle = needle;
    }

    /**
     * @param needle       the tenant id to look for, as a substring of any String
     * @param roots        label to root object; the label starts every path under it
     * @param staticOwners classes whose static fields are roots too (a class reached by an instance is read anyway)
     */
    static Result walk(String needle, Map<String, Object> roots, Collection<Class<?>> staticOwners) {
        if (needle == null || needle.length() < 6) {
            throw new IllegalArgumentException("a needle this short would match unrelated strings: " + needle);
        }
        var w = new TenantLeakWalker(needle);
        for (Class<?> c : staticOwners) {
            w.readStatics(c);
        }
        roots.forEach((label, root) -> w.todo.push(new Node(label, root, 0)));
        w.run();
        return new Result(List.copyOf(w.hits), w.objects, w.staticsRead.size(), List.copyOf(w.staticSkipped),
            new TreeMap<>(w.opaque));
    }

    private void run() {
        while (!todo.isEmpty() && objects < MAX_OBJECTS && hits.size() < MAX_HITS) {
            Node n = todo.pop();
            visit(n);
        }
    }

    private void visit(Node n) {
        Object v = n.value();
        if (v == null || n.depth() > MAX_DEPTH) {
            return;
        }
        if (v instanceof String s) {
            if (s.contains(needle)) {
                hits.add(new Hit(n.path(), s.length() > 120 ? s.substring(0, 120) + "..." : s));
            }
            return;
        }
        if (v instanceof Number || v instanceof Boolean || v instanceof Character || v instanceof Enum<?>
            || v instanceof Class<?> || v instanceof CharSequence) {
            return;
        }
        if (!seen.add(v)) {
            return;
        }
        objects++;
        if (v instanceof Map<?, ?> m) {
            for (Map.Entry<?, ?> e : m.entrySet()) {
                push(n, "{key}", e.getKey());
                push(n, "[" + e.getKey() + "]", e.getValue());
            }
        } else if (v instanceof Collection<?> c) {
            int i = 0;
            for (Object e : c) {
                push(n, "[" + i++ + "]", e);
            }
        } else if (v instanceof Object[] a) {
            for (int i = 0; i < a.length; i++) {
                push(n, "[" + i + "]", a[i]);
            }
        } else if (v instanceof Optional<?> o) {
            o.ifPresent(x -> push(n, ".get", x));
        } else if (v instanceof AtomicReference<?> r) {
            push(n, ".get", r.get());
        } else if (v.getClass().isArray()) {
            return;   // primitive arrays: no tenant name in a byte[] this walk can read as a String
        } else if (v.getClass().getName().startsWith("dev.nexus.")) {
            fields(n, v);
            readStatics(v.getClass());
        } else {
            opaque.merge(v.getClass().getName(), 1, Integer::sum);
        }
    }

    private void push(Node parent, String step, Object child) {
        if (child != null) {
            todo.push(new Node(parent.path() + step, child, parent.depth() + 1));
        }
    }

    private void fields(Node n, Object v) {
        for (Class<?> c = v.getClass(); c != null && c.getName().startsWith("dev.nexus."); c = c.getSuperclass()) {
            for (Field f : c.getDeclaredFields()) {
                if (Modifier.isStatic(f.getModifiers()) || f.getType().isPrimitive()) {
                    continue;
                }
                try {
                    f.setAccessible(true);
                    push(n, "." + f.getName(), f.get(v));
                } catch (ReflectiveOperationException | RuntimeException e) {
                    staticSkipped.add("field " + c.getName() + "." + f.getName() + ": " + e);
                }
            }
        }
    }

    /** Reads the static reference fields of {@code c} once; the path starts at {@code Class.field}. */
    private void readStatics(Class<?> c) {
        if (!staticsRead.add(c)) {
            return;
        }
        String label = c.getName().substring(c.getName().lastIndexOf('.') + 1);
        try {
            for (Field f : c.getDeclaredFields()) {
                if (!Modifier.isStatic(f.getModifiers()) || f.getType().isPrimitive()) {
                    continue;
                }
                f.setAccessible(true);
                Object value = f.get(null);
                if (value != null) {
                    todo.push(new Node(label + "." + f.getName(), value, 0));
                }
            }
        } catch (ReflectiveOperationException | LinkageError | RuntimeException e) {
            staticsRead.remove(c);
            staticSkipped.add("statics of " + c.getName() + ": " + e);
        }
    }
}
