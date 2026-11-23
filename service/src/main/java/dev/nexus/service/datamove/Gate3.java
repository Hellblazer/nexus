// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

/**
 * RDR-229 item 6, "Readers during the stamp": whether gate 3 of the completion check has LATCHED on this install, the one signal
 * P3.5a ({@code CollectionRegistry}'s callers) and P3.5c (every Java reader and upsert of a collection name column) switch to the
 * id on. The value is the latched one, and it is monotone: once gate 3 has held on an install it stays true, through restarts and
 * through a later failure of the live gate 3 (an id index going INVALID is reported apart, as {@code id_index_invalid}, and the
 * reconciler rebuilds it), because after P3.7's first one-row rename the name columns are stale and a reader must never go back to
 * them (linda 2026-11-23; P3.4's {@code PciIdGate3IntegrationTest} pins that the latched value stays true).
 *
 * <p>{@code Main} builds the production instance: the boot latch ({@code Gate3Latch.readAtBoot}, read before the service is built)
 * until the completion check starts, then {@link CompletionBootWork#gate3Latched()}; it is passed by constructor to each reader and
 * read on every call. Tests pass {@link #never()} or {@code () -> true}.
 */
@FunctionalInterface
public interface Gate3 {

    /** Whether gate 3 holds now. Read on each call; an implementation must not cache the answer. */
    boolean holds();

    /** Gate 3 never holds: the name key for every read, as before P3.5. */
    static Gate3 never() {
        return () -> false;
    }
}
