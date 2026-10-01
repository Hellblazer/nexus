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
 * <p>The setting is the environment variable {@value #ENV}. An UNSET (or blank) value means
 * {@link #LOG_ONLY}; only an explicit {@code enforce} enforces (conexus's condition for the first
 * production deploy: the engine's first run against real traffic must not refuse anything until the
 * would-refuse log has been read). Local installs enforce regardless, because the local engine
 * launch path sets {@code enforce} explicitly ({@code nexus.daemon.storage_service_daemon}). An
 * unrecognised value fails the boot rather than silently choosing a mode.
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
     * Parse the setting value. {@code null} or blank is the default ({@link #LOG_ONLY}).
     *
     * @throws IllegalArgumentException for any other unrecognised value
     */
    public static OwnerlessWriteMode parse(String raw) {
        if (raw == null || raw.isBlank()) {
            return LOG_ONLY;
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

    /** Reads the setting; replaced by tests, which cannot change the real process environment. */
    private static volatile java.util.function.Function<String, String> envReader = System::getenv;

    /** Test seam: {@code null} restores the real environment. */
    public static void setEnvReaderForTests(java.util.function.Function<String, String> reader) {
        envReader = reader != null ? reader : System::getenv;
    }

    /** The mode the process environment selects. */
    public static OwnerlessWriteMode fromEnv() {
        return parse(envReader.apply(ENV));
    }
}
