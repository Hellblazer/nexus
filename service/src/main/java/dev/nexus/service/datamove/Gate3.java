// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

/**
 * RDR-229 item 6, "Readers during the stamp": whether gate 3 of the completion check holds on this install, the one signal
 * P3.5a ({@code CollectionRegistry}) and P3.5c (every repository whose upsert conflicts on a name key) switch to the id on.
 *
 * <p>True once the latest completion check's gate 3 holds; never cached past one call. Main builds one instance over
 * {@link CompletionBootWork#latest()} and passes it by constructor to each reader, so the switch needs no restart. It can
 * drop back to false (an id index going INVALID makes gate 3 fail again); a reader then returns to the name key, which is
 * safe because both keys exist until P4.1 drops the name columns. Tests pass {@link #never()} or {@code () -> true}.
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
