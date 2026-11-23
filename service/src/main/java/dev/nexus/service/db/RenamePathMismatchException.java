// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * A rename reached the one-row regime (the gate 3 latch row exists and the rename readiness is complete) with no one-row rename
 * to run: the catalog rename's {@link OneRowRename} is the fail-closed default (RDR-229 P3.5a, linda's R-A ruling). Raised inside the
 * rename's transaction before anything is written; {@link dev.nexus.service.http.HttpUtil#sendTypedDbError} answers 409
 * {@code rename_path_mismatch}. Running the name path here would rewrite name columns the one-row regime's keys depend on.
 */
public final class RenamePathMismatchException extends RuntimeException {

    public RenamePathMismatchException(String oldLabel, String newLabel) {
        super("a rename of '" + oldLabel + "' to '" + newLabel + "' reached the one-row regime, and this engine has no one-row "
            + "catalog rename to run; nothing was renamed");
    }
}
