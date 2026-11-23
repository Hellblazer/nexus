// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

/**
 * RDR-229 item 6, "Readers during the stamp": whether gate 3 of the completion check holds on this install, the one signal
 * P3.5a ({@code CollectionRegistry}) and P3.5c (every repository whose upsert conflicts on a name key) switch to the id on.
 *
 * <p>The LATCHED gate 3: persisted and monotone (linda 2026-11-23), so once true it stays true and a reader or upsert never goes back to
 * a name key. Never cached past one call in the engine: Main builds one instance over the latched read
 * ({@code CompletionBootWork#gate3Latched()}) and passes it by constructor to each reader, so the switch needs no restart. Tests pass
 * {@link #never()} or {@code () -> true}.
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
