// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

/**
 * RDR-229 item 6, "Readers during the stamp": whether gate 3 of the completion check holds on this install, the one signal
 * P3.5a ({@code CollectionRegistry}) and P3.5c (every repository whose upsert conflicts on a name key) switch to the id on.
 *
 * <p>The latched value (linda 2026-11-22, {@code CompletionBootWork#gate3Latched()}): false until gate 3 first held on the install,
 * true from then on, and it never goes back (an id index going INVALID later does not unlatch it). Main builds one instance over that
 * latch and passes it by constructor to each reader, so the switch needs no restart. Because it is monotone, an operation that reads
 * it more than once may see false and then true, never true and then false; a reader that read true may rely on it for the rest of its
 * statement. Tests pass {@link #never()} or a fixed true; production code passes the one held gate on, never a literal.
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
