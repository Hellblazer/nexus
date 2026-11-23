// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

/**
 * RDR-229 P3.5a: what a collection rename still waits for. The rename guard reads it with the gate 3 latch row: the row decides the
 * path, except that no row with the readiness complete ({@link Missing#NONE}) is a refusal, {@code latch_row_missing}; with the row,
 * the primitive refuses a rename until the name-key drop move has completed and every P3.5b changeset has applied (which needs
 * a restart), naming the missing one. Before the latch row it is read only to tell the name path from that inconsistency.
 *
 * <p><b>Contract</b> (linda): total, cheap and fail-closed. {@link #missing()} never throws, and any read failure or timeout answers a
 * value other than {@code NONE} that names the missing condition (for example an "unreadable" value, with a short
 * {@code statement_timeout} on its reads), so a failure is never read as "complete". That behaviour is P3.7's. The drop move is gated
 * on the latched gate 3, so {@code NONE} implies the latch row exists; "no row, readiness {@code NONE}" is a real inconsistency (a
 * partial restore), never a timing window.
 *
 * <p>Built once in Main from the same {@link CompletionCheck} snapshot that {@link Gate3} reads (the move rows and the
 * applied changesets), never a second reader of {@code nexus.data_moves}; constructor-injected beside {@code Gate3}, and
 * read per call, never cached past one call.
 */
@FunctionalInterface
public interface RenameReadiness {

    /** The first thing a rename still waits for, or {@link Missing#NONE}. */
    Missing missing();

    /** What a rename from gate 3 waits for; the wire value is {@code missing} on the 409 {@code rename_refused}. */
    enum Missing {
        /** Nothing: a rename may run. */
        NONE,
        /** The name-key drop move (P3.7) has not completed. Wire value {@code name_key_drop_move}. */
        NAME_KEY_DROP_MOVE,
        /**
         * The move completed but this build's P3.5b changeset list ({@code db/rdr229/p35b-continue-changesets.txt}) is missing or names
         * nothing, so the engine cannot tell whether the readers are ready and a restart cannot fix it. Wire value {@code p35b_list_missing}.
         */
        P35B_LIST_MISSING,
        /** The move completed but a P3.5b changeset has not applied yet: the engine must restart. Wire value {@code restart}. */
        P35B_RESTART,
        /**
         * The readiness could not be read (no sample yet, a read that failed or ran past {@link CompletionCheck#READINESS_READ_TIMEOUT_MS}, a list that
         * could not be read): it never answers NONE from an error. Before the latch it is not consulted (a rename is the name path); after it, the rename is
         * refused. Wire value {@code readiness_unreadable} (linda 2026-11-23).
         */
        READINESS_UNREADABLE,
        /**
         * Everything the one-row rename waits for is complete but this engine's readers still select by name ({@code IdReaders.present()} is false: P3.5c
         * flips it), so a one-row rename would leave a reader on a label that no longer names the collection. Wire value {@code p35c_readers_absent}
         * (linda's census of 19d19081dc).
         */
        P35C_READERS_ABSENT,
        /**
         * Not a readiness answer: the rename guard's own refusal when the readiness says {@link #NONE} (the move is done) and there is no
         * gate 3 latch row, an inconsistency such as a partial restore. Wire value {@code latch_row_missing}.
         */
        LATCH_ROW_MISSING
    }

    /**
     * A readiness that says the move is complete, for tests of the one-row regime. With no latch row the rename guard refuses it
     * ({@code latch_row_missing}), so it is not a readiness for a store that has no latch.
     */
    static RenameReadiness ready() {
        return () -> Missing.NONE;
    }
}
