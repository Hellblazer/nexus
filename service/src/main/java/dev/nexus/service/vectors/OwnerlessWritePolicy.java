// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.util.Objects;

/**
 * Holder of the {@link OwnerlessWriteMode} the vector handler applies. Production sets it once
 * from the environment ({@link #fromEnv()}) and never changes it; the holder is mutable only so a
 * test can flip a running service between modes without rebuilding it.
 */
public final class OwnerlessWritePolicy {

    private volatile OwnerlessWriteMode mode;

    public OwnerlessWritePolicy(OwnerlessWriteMode mode) {
        this.mode = Objects.requireNonNull(mode, "mode");
    }

    /** A policy initialised from {@value OwnerlessWriteMode#ENV}; an invalid value fails here. */
    public static OwnerlessWritePolicy fromEnv() {
        return new OwnerlessWritePolicy(OwnerlessWriteMode.fromEnv());
    }

    public OwnerlessWriteMode mode() {
        return mode;
    }

    /** Test seam: production never calls this after boot. */
    public void set(OwnerlessWriteMode next) {
        this.mode = Objects.requireNonNull(next, "mode");
    }
}
