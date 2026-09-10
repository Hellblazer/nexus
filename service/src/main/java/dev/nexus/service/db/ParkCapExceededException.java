// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * RDR-205 typed error: a blocking {@code rd}/{@code in} could not park — the
 * per-claimant cap (default 4) or the engine-wide cap (default 16) is
 * already at capacity. Thrown only at the moment of attempting to ENTER the
 * park loop, after the call's own immediate (non-blocking) probe already
 * found nothing — so "the caller gets the probe result and backs off"
 * (RDR-205 §Technical Design "Wake") is always an empty probe result here;
 * {@code TupleHandler} renders the same empty shape a plain {@code
 * rdp}/{@code inp} miss would, alongside this typed error.
 */
public final class ParkCapExceededException extends TupleException {

    /** Which cap was at capacity: {@code "claimant"} or {@code "global"}. */
    private final String scope;

    public ParkCapExceededException(String scope) {
        super("ParkCapExceeded", 429,
                "park cap exceeded (" + scope + "); back off and retry");
        this.scope = scope;
    }

    public String scope() {
        return scope;
    }
}
