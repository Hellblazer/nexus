// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.datamove.RenameReadiness;

/**
 * A collection rename was refused because gate 3 holds and the one-row rename cannot run yet (RDR-229 P3.5a, bead
 * nexus-p2m1x.23; RDR item 6, "Readers during the stamp"): the name-key drop move has no {@code completed_at}, or it has and a
 * P3.5b changeset has not applied, which takes a restart. Raised by {@link RenameGuard} before anything is written;
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
            case P35B_RESTART -> "a collection rename is refused until the engine restarts to apply the P3.5b changesets; "
                + "the restart is the missing condition";
            case NONE -> "";
        };
    }

    /** The missing condition. */
    public RenameReadiness.Missing missing() {
        return missing;
    }

    /** The wire value of {@code missing}: {@code name_key_drop_move} or {@code restart}. */
    public String wireValue() {
        return wireValue(missing);
    }

    /** The wire value of a missing condition; {@code ""} for {@link RenameReadiness.Missing#NONE}. */
    public static String wireValue(RenameReadiness.Missing missing) {
        return switch (missing) {
            case NAME_KEY_DROP_MOVE -> "name_key_drop_move";
            case P35B_RESTART -> "restart";
            case NONE -> "";
        };
    }
}
