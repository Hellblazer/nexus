// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * A per-store rename (chash, taxonomy, telemetry, aspects, highlights) ran in the one-row regime, where it is a no-op because the
 * registry rename has already moved the label, and the registry says it has not: the new label is not a live registry row, or the old
 * one still is (RDR-229 P3.5a, linda's Q-A ruling). Raised in the rename's transaction before anything is written;
 * {@link dev.nexus.service.http.HttpUtil#sendTypedDbError} answers 409 {@code rename_not_in_registry}.
 */
public final class RenameNotInRegistryException extends RuntimeException {

    public RenameNotInRegistryException() {
        super("rename the collection in the registry first; the label pair given does not match it");
    }
}
