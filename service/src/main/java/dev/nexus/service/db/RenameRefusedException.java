// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.datamove.RenameReadiness;

/**
 * A collection rename was refused because gate 3 holds and the one-row rename cannot run yet (RDR-229 P3.5a, bead
 * nexus-p2m1x.23; RDR item 6, "Readers during the stamp"): the name-key drop move has no {@code completed_at}, or it has and this
 * build ships no P3.5b changeset list ({@code p35b_list_missing}), or the list names a changeset that has not applied, which takes a restart. Raised by {@link RenameGuard} before anything is written;
 * {@link dev.nexus.service.http.HttpUtil#sendTypedDbError} answers a typed 409 {@code rename_refused} whose {@code missing} is
 * {@link #wireValue()}.
 */
public final class RenameRefusedException extends RuntimeException {

    private final RenameReadiness.Missing missing;

    public RenameRefusedException(RenameReadiness.Missing missing) {
        super(messageFor(missing));
        if (missing == RenameReadiness.Missing.NONE) {
            throw new IllegalArgumentException("a refusal names a missing condition; NONE is not one");
        }
        this.missing = missing;
    }

    private static String messageFor(RenameReadiness.Missing missing) {
        return switch (missing) {
            case NAME_KEY_DROP_MOVE -> "a collection rename is refused until the name-key drop move has completed on this "
                + "install; the move is the missing condition";
            case P35B_LIST_MISSING -> "a collection rename is refused because this build ships no P3.5b changeset list "
                + "(db/rdr229/p35b-continue-changesets.txt is missing or names nothing); a restart does not fix it, a build with the list does";
            case P35B_RESTART -> "a collection rename is refused until the engine restarts to apply the P3.5b changesets; "
                + "the restart is the missing condition";
            case READINESS_UNREADABLE -> "a collection rename is refused because what it waits for could not be read; the readiness is the "
                + "missing condition, and it is read again at the next sample";
            case NONE -> "";
        };
    }

    /** The missing condition. */
    public RenameReadiness.Missing missing() {
        return missing;
    }

    /** The wire value of {@code missing}: {@code name_key_drop_move}, {@code p35b_list_missing}, {@code restart} or {@code readiness_unreadable}. */
    public String wireValue() {
        return wireValue(missing);
    }

    /** The wire value of a missing condition; {@code ""} for {@link RenameReadiness.Missing#NONE}. */
    public static String wireValue(RenameReadiness.Missing missing) {
        return switch (missing) {
            case NAME_KEY_DROP_MOVE -> "name_key_drop_move";
            case P35B_LIST_MISSING -> "p35b_list_missing";
            case P35B_RESTART -> "restart";
            case READINESS_UNREADABLE -> "readiness_unreadable";
            case NONE -> "";
        };
    }
}
