// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import java.util.Map;

/**
 * The topics of {@code board/ci/<topic>} that a {@code board-ci}-scoped credential may not write (RDR-252 Step 1).
 * The scope is per template, so it reaches every {@code board/ci/<topic>} subspace, and its token sits in a public
 * Lambda; the push, owner, clear and ack rows come from sessions and the engine's stamper on the {@code --push}
 * and {@code --owner} topics, so a {@code board-ci} write to either is refused and the adapter keeps its own posts.
 *
 * <p>The topic judged is the one actually written. The subspace names it and so does the {@code topic} key, and
 * the template resolves from the subspace alone, so a writer could post to {@code board/ci/nexus-develop} with
 * {@code topic=nexus-develop--owner}: both are checked, and a pair that disagrees is refused.
 */
final class BoardCiGuard {

    static final String PREFIX = "board/ci/";
    private static final String[] FENCED_SUFFIXES = {"--owner", "--push"};

    private BoardCiGuard() {
    }

    /** The {@code from} value only the engine's own stamper writes (RDR-252): a client write that carries it is refused. */
    static final String RESERVED_FROM = "engine";
    /** The error code a refused write carries. */
    static final String RESERVED_DIM = "reserved_dim";

    /**
     * Why a CLIENT write (any credential, through the HTTP route) to a {@code board/ci/<topic>} subspace is refused
     * for its dims, or null when it passes. {@code from=engine} marks the owner and clear rows the stamper wrote; the
     * stamper writes through the repository, never through the route, so no client write may carry it. A missing
     * sender session cannot tell the engine's row from a raw-HTTP client's (RDR-252 S3), so the dim is made
     * engine-only by construction.
     */
    static String reservedDimRefusal(Map<String, String> dims) {
        return dims != null && RESERVED_FROM.equals(dims.get("from"))
            ? RESERVED_DIM + ": the dim from=" + RESERVED_FROM + " on a board/ci topic is written by the engine alone" : null;
    }

    /** Why a {@code board-ci} write to {@code subspace} with these keys is refused, or null when it passes. */
    static String refusal(String subspace, Map<String, String> keys) {
        String segment = subspace.startsWith(PREFIX) ? subspace.substring(PREFIX.length()) : subspace;
        String keyTopic = keys == null ? null : keys.get("topic");
        if (keyTopic != null && !keyTopic.equals(segment)) {
            return "the topic key (" + keyTopic + ") and the subspace (" + segment + ") name different topics";
        }
        for (String suffix : FENCED_SUFFIXES) {
            if (segment.endsWith(suffix)) {
                return "a topic ending " + suffix + " is written by sessions and the engine, not by the CI adapter";
            }
        }
        return null;
    }
}
