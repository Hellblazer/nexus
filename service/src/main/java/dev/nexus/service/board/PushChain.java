// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.time.Instant;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Develop's tips in commit order (RDR-252 "Develop's state from commit order"). Push records link each {@code base}
 * to its tip, and the chain, never arrival time, orders two tips it links. A tip that a verdict names but no push
 * record does is a node too (a hand push, a back-merge), found only through its CI post.
 *
 * <p>A node whose {@code base} matches no node starts a segment. Segments are placed by the time of their first node
 * (the push record's, or the CI post's for a node with no record), and each segment but the first is marked
 * {@code gapBefore}: the chain has a gap there, and what lies past it is ordered by the posts' own times, best
 * effort. A base that equals a verdict-only node links to it, so a green tip with no push record still anchors the
 * pushes after it without a gap.
 */
public final class PushChain {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    /** A tip: its sha, its push record (null when none names it), and whether the chain has a gap before it. */
    public record Node(String sha, BoardRow push, boolean gapBefore) {
        public boolean recorded() {
            return push != null;
        }
    }

    private PushChain() {
    }

    /**
     * The tips in commit order.
     *
     * @param pushes        the rows of the {@code --push} subspace
     * @param verdictTimes  for each sha with a success or failure verdict, the time of its newest verdict post
     */
    public static List<Node> order(List<BoardRow> pushes, Map<String, Instant> verdictTimes) {
        Map<String, BoardRow> recorded = new LinkedHashMap<>();
        Map<String, String> baseOf = new LinkedHashMap<>();
        List<BoardRow> sorted = new ArrayList<>(pushes);
        sorted.sort(Comparator.comparing(BoardRow::createdAt).thenComparing(BoardRow::id));
        for (BoardRow p : sorted) {
            String sha = p.sha();
            if (sha == null || sha.isBlank() || recorded.containsKey(sha)) {
                continue;
            }
            recorded.put(sha, p);
            baseOf.put(sha, baseOf(p));
        }
        Map<String, Instant> key = new LinkedHashMap<>();
        recorded.forEach((sha, p) -> key.put(sha, p.createdAt()));
        verdictTimes.forEach((sha, at) -> key.putIfAbsent(sha, at));

        Map<String, String> parent = new LinkedHashMap<>();
        Map<String, List<String>> children = new LinkedHashMap<>();
        baseOf.forEach((sha, base) -> {
            if (base != null && !base.equals(sha) && key.containsKey(base)) {
                parent.put(sha, base);
                children.computeIfAbsent(base, b -> new ArrayList<>()).add(sha);
            }
        });
        Comparator<String> byKey = Comparator.<String, Instant>comparing(key::get).thenComparing(s -> s);
        children.values().forEach(c -> c.sort(byKey));

        List<String> roots = new ArrayList<>();
        key.keySet().stream().filter(s -> !parent.containsKey(s)).sorted(byKey).forEach(roots::add);
        List<Node> out = new ArrayList<>();
        Set<String> seen = new HashSet<>();
        boolean first = true;
        for (String root : roots) {
            walk(root, !first, recorded, children, seen, out);
            first = false;
        }
        // A cycle of bases (never produced by a push) has no root; place what is left by time, each as a gap.
        key.keySet().stream().filter(s -> !seen.contains(s)).sorted(byKey)
            .forEach(s -> walk(s, true, recorded, children, seen, out));
        return out;
    }

    private static void walk(String start, boolean gapBefore, Map<String, BoardRow> recorded,
                             Map<String, List<String>> children, Set<String> seen, List<Node> out) {
        // Depth-first, children in time order; iterative, so a long chain cannot overflow the stack.
        java.util.ArrayDeque<String> stack = new java.util.ArrayDeque<>();
        stack.push(start);
        boolean gap = gapBefore;
        while (!stack.isEmpty()) {
            String sha = stack.pop();
            if (!seen.add(sha)) {
                continue;
            }
            out.add(new Node(sha, recorded.get(sha), gap));
            gap = false;
            List<String> kids = children.getOrDefault(sha, List.of());
            for (int i = kids.size() - 1; i >= 0; i--) {
                stack.push(kids.get(i));
            }
        }
    }

    static String baseOf(BoardRow push) {
        try {
            JsonNode body = push.body() == null ? null : MAPPER.readTree(push.body());
            return body != null && body.hasNonNull("base") ? body.get("base").asText() : null;
        } catch (java.io.IOException e) {
            return null;
        }
    }
}
