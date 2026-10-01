// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.util.Locale;

/**
 * RDR-223 Phase 3 Step 2 (nexus-z0o2p.24): what the engine does with a chunk write on
 * {@code /v1/vectors/upsert-chunks} or {@code /store-put} whose chashes are not all owned
 * (a chash with no live manifest row in the collection).
 *
 * <ul>
 *   <li>{@link #ENFORCE} refuses the whole request with 422, naming the combined write routes;</li>
 *   <li>{@link #LOG_ONLY} writes it exactly as before, but logs one {@code
 *       ownerless_chunk_write_would_refuse} line and counts it, so an operator can list every
 *       writer the refusal would break before switching it on.</li>
 * </ul>
 *
 * <p>The setting is the environment variable {@value #ENV}. The default is {@link #ENFORCE}:
 * log-only exists to prove the writer list empty (or dispositioned) and is not a posture to
 * run in. An unrecognised value fails the boot rather than silently choosing a mode.
 */
public enum OwnerlessWriteMode {
    LOG_ONLY("log-only"),
    ENFORCE("enforce");

    /** Environment variable that selects the mode. */
    public static final String ENV = "NX_OWNERLESS_WRITE_MODE";

    private final String wire;

    OwnerlessWriteMode(String wire) {
        this.wire = wire;
    }

    /** The setting's spelling, also the value {@code /v1/status} reports. */
    public String wire() {
        return wire;
    }

    /**
     * Parse the setting value. {@code null} or blank is the default ({@link #ENFORCE}).
     *
     * @throws IllegalArgumentException for any other unrecognised value
     */
    public static OwnerlessWriteMode parse(String raw) {
        if (raw == null || raw.isBlank()) {
            return ENFORCE;
        }
        String v = raw.strip().toLowerCase(Locale.ROOT).replace('_', '-');
        for (OwnerlessWriteMode m : values()) {
            if (m.wire.equals(v)) {
                return m;
            }
        }
        throw new IllegalArgumentException(
            ENV + " must be 'enforce' or 'log-only', got '" + raw + "'");
    }

    /** The mode the process environment selects. */
    public static OwnerlessWriteMode fromEnv() {
        return parse(System.getenv(ENV));
    }
}
