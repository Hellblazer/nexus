// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

/**
 * RDR-229 item 6, "Readers during the stamp": whether gate 3 of the completion check holds on this install, the one signal
 * P3.5a ({@code CollectionRegistry}) and P3.5c (every repository whose upsert conflicts on a name key) switch to the id on.
 *
 * <p>True once gate 3 has LATCHED on the install (linda 2026-11-23): the latch is persisted and monotone, so once this holds it never
 * returns to false. It is read on every call, never cached past one, and Main builds one instance over
 * {@link CompletionBootWork#gate3Latched()} and passes it by constructor to each reader, so the switch needs no restart. A reader
 * may therefore assume that the name path is not coming back: after a one-row rename the name columns are stale. Tests pass
 * {@link #never()} (the state before the latch) or {@code () -> true}.
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
