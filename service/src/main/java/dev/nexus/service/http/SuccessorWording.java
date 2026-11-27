// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import java.util.Optional;

/**
 * RDR-229 R7 (bead nexus-p2m1x.25): what a refusal says a retired collection was superseded by. The successor is the live row found through
 * {@code CollectionSuccessor}, named by its current label; a tombstone whose successor was deleted names none, and the refusal says so
 * instead of printing the dead label its {@code superseded_by} text still holds.
 */
final class SuccessorWording {

    private static final String NONE = "retired and names no live successor";

    private SuccessorWording() {
    }

    /** {@code superseded by <label>}, or {@code retired and names no live successor}. */
    static String supersededBy(Optional<String> live) {
        return live.map(l -> "superseded by " + l).orElse(NONE);
    }

    /** For {@code "is retired (...)"}: {@code superseded by <label>}, or {@code it names no live successor}. */
    static String retiredBecause(Optional<String> live) {
        return live.map(l -> "superseded by " + l).orElse("it names no live successor");
    }

    /** {@code superseded to <label>}, or {@code retired and names no live successor}. */
    static String supersededTo(Optional<String> live) {
        return live.map(l -> "superseded to " + l).orElse(NONE);
    }

    /** The 409 for a supersede that found, after its UPDATE matched nothing, that {@code name} had been retired meanwhile. */
    static String concurrentSupersede(String name, Optional<String> live, String requestedSuccessor) {
        return "collection " + name + " was " + supersededTo(live) + " concurrently; refusing to chain a second supersede to "
            + requestedSuccessor;
    }
}
