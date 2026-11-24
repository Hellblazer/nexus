// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

/**
 * RDR-229 P3.5's switch (defined by P3.4, nexus-p2m1x.22): whether this engine's readers select by {@code collection_id}.
 *
 * <p>{@link #PRESENT} is {@code true} (P3.5c, in the same engine cut as the id readers): from the latched gate 3 the Java readers read
 * {@code collection_id} and the search statements call the {@code <name>_by_id} twins, so the name-index retire ({@code REPLACED_BY_ID},
 * {@code PciReconcilePlanner}) may run once the latch holds. It stays {@code false} for as long as any reader still selects by name after the
 * latch: dropping the name-predicate per-collection indexes would leave that reader without them. {@code Rdr229P35cIdReadersPinTest} holds
 * the value to what the census readers' dispositions say: it fails on a census reader with no disposition and on one whose disposition is
 * not an id read (only {@code id} and {@code both} count; {@code label-kept} is the registry's own label). It does not see the by-label SQL
 * calls of the reaper and the chunk reaper (the 5 {@code holds-until-P4.1} rows of {@code p35c-i8-stored-label-census.tsv}, nexus-p2m1x.35): those are P4.1's to retire before the
 * name columns go. {@link PciIdBuilds} reads it through {@link #present()}; a test controls the
 * reconciler through its injectable supplier.
 */
public final class IdReaders {

    private IdReaders() { }

    /** True once this engine's readers select by id (P3.5c). */
    public static final boolean PRESENT = true;

    /**
     * {@link #PRESENT}, read through a method so a caller compiled against an older value never holds an inlined copy of the constant: the
     * production default of the retire's guard calls this, and a recompile of this class alone is enough to flip it.
     */
    public static boolean present() {
        return PRESENT;
    }
}
