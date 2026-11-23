// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

/**
 * RDR-229 P3.5's switch (defined by P3.4, nexus-p2m1x.22): whether this engine's readers select by {@code collection_id}.
 *
 * <p>While {@link #PRESENT} is {@code false} (this release) the name-index retire ({@code REPLACED_BY_ID}, {@code PciReconcilePlanner}) never
 * runs, whatever the gate 3 latch says: the readers still select by name, and dropping the name-predicate per-collection indexes would leave
 * search without them. P3.5c, in the same engine cut as the id readers, flips it to {@code true}. {@link PciIdBuilds} reads it through
 * {@link #present()}; a test controls the reconciler through its injectable supplier.
 */
public final class IdReaders {

    private IdReaders() { }

    /** True once this engine's readers select by id (P3.5c flips it). */
    public static final boolean PRESENT = false;

    /**
     * {@link #PRESENT}, read through a method so a caller compiled against an older value never holds an inlined copy of the constant: the
     * production default of the retire's guard calls this, and a recompile of this class alone is enough to flip it.
     */
    public static boolean present() {
        return PRESENT;
    }
}
