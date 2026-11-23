// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.datamove.Gate3;

import java.util.Objects;
import java.util.function.BooleanSupplier;

/**
 * The latch the recall canary follows (RDR-229 P3.4, linda 2026-11-23): the reconciler's own {@code idBuilds.gate3Holds()} (gate 3 latched and the id
 * readers present, what the retire reads), bound once the reconciler exists and read at each call, never captured. Until bound it never holds.
 * The canary's id statements, its ring reader and the query ring's admission all read this one object.
 */
final class PciCanaryLatch implements Gate3 {

    private volatile BooleanSupplier source = () -> false;

    void follow(BooleanSupplier latched) {
        this.source = Objects.requireNonNull(latched, "latched");
    }

    @Override
    public boolean holds() {
        return source.getAsBoolean();
    }
}
