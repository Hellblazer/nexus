// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

/**
 * RDR-229 P3.5a: what a collection rename still waits for once gate 3 holds ({@link Gate3}). From gate 3 the shared rename
 * primitive refuses a rename until the name-key drop move has completed and every P3.5b changeset has applied (which needs
 * a restart), naming the missing one; before gate 3 the rename is the name path and this is not consulted.
 *
 * <p>Built once in Main from the same {@link CompletionCheck} snapshot that {@link Gate3} reads (the move rows and the
 * applied changesets), never a second reader of {@code nexus.data_moves}; constructor-injected beside {@code Gate3}, and
 * read per call, never cached past one call.
 */
@FunctionalInterface
public interface RenameReadiness {

    /** The first thing a rename still waits for, or {@link Missing#NONE}. */
    Missing missing();

    /**
     * RDR-229 P3.7, the rename window (fix check A O-5): runs {@code rename}, a whole rename including its commit, with gate 3 unable to
     * flip from false to true underneath it. A name-path rename that began before gate 3 held would otherwise commit after the id readers
     * switched, and give the collection a new id they have not seen. The completion check holds the flip until every rename pinned
     * here has returned ({@link CompletionBootWork#renameReadiness()}); a rename that starts after the flip reads gate 3 as true and is
     * refused or runs one-row. The default runs {@code rename} as is, for a readiness that has no window ({@link #ready()}, a test's lambda).
     */
    default <T> T pinned(java.util.function.Supplier<T> rename) {
        return rename.get();
    }

    /** What a rename from gate 3 waits for; the wire value is {@code missing} on the 409 {@code rename_refused}. */
    enum Missing {
        /** Nothing: a rename may run. */
        NONE,
        /** The name-key drop move (P3.7) has not completed. Wire value {@code name_key_drop_move}. */
        NAME_KEY_DROP_MOVE,
        /** The move completed but a P3.5b changeset has not applied yet: the engine must restart. Wire value {@code restart}. */
        P35B_RESTART
    }

    /** A readiness that never waits, for tests and for a store gate 3 never reaches. */
    static RenameReadiness ready() {
        return () -> Missing.NONE;
    }
}
