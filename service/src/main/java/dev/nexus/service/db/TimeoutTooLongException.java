// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * RDR-205 typed error: {@code timeout_s} above the engine's cap (default 25s,
 * CA 3 — the edge times out a response that has not started within 30s).
 */
public final class TimeoutTooLongException extends TupleException {

    private final long timeoutSeconds;
    private final long capSeconds;

    public TimeoutTooLongException(long timeoutSeconds, long capSeconds) {
        super("TimeoutTooLong", 400,
                "timeout_s " + timeoutSeconds + " exceeds the engine cap of " + capSeconds + "s");
        this.timeoutSeconds = timeoutSeconds;
        this.capSeconds = capSeconds;
    }

    public long timeoutSeconds() {
        return timeoutSeconds;
    }

    public long capSeconds() {
        return capSeconds;
    }
}
