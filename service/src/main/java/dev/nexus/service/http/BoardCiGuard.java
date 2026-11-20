// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.LinkedHashMap;
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

    private static final Logger log = LoggerFactory.getLogger(BoardCiGuard.class);

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

    private static final String OWNER_SUFFIX = "--owner";

    /**
     * RDR-252 (nexus-igea9.4): the dims a {@code kind=ack} client write to a {@code board/ci/<topic>--owner} subspace
     * lands with. The owner decision counts an ack by the session stamp the engine writes, never by {@code from}, so
     * {@code from} is only the label that {@code nx group ack-times} and people read; it is made true. The caller gives
     * the identity the decision would count the row's stamp for ({@link dev.nexus.service.board.AckIdentity}: the
     * session index's identity, a delegated one as its parent), with or without an identity token. {@code from} is
     * set to it: filled when absent or blank, overwritten (and logged at info with the old and the new value) when it
     * differs, left alone when it matches. Nothing is refused. With no identity (the stamp maps to none) the dims pass
     * unchanged, so a {@code from} that survives is one no counted ack carries. Returns {@code dims} itself when
     * nothing is changed.
     */
    static Map<String, String> stampAckFrom(String subspace, Map<String, String> dims, String countedIdentity) {
        if (countedIdentity == null) {
            return dims;
        }
        String from = dims.get("from");
        if (countedIdentity.equals(from)) {
            return dims;
        }
        if (from != null && !from.isBlank()) {
            log.info("event=board_ack_from_overwritten subspace={} from={} identity={}", LogText.escapeControl(subspace),
                LogText.escapeControl(from), countedIdentity);
        }
        Map<String, String> stamped = new LinkedHashMap<>(dims);
        stamped.put("from", countedIdentity);
        return stamped;
    }

    /**
     * True for a {@code kind=ack} write to a {@code board/ci/<topic>--owner} subspace: the only write
     * {@link #stampAckFrom} acts on. A subspace under another template that ends {@code --owner} is not one.
     */
    static boolean isOwnerAck(String subspace, Map<String, String> dims) {
        return dims != null && "ack".equals(dims.get("kind")) && subspace.startsWith(PREFIX) && subspace.endsWith(OWNER_SUFFIX);
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
