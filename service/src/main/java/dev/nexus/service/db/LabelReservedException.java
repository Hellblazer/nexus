// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * A label could not be made live because it is RESERVED (RDR-229 P3.7, linda 2026-11-23): a collection renamed away from it, by the one-row rename, still has
 * rows in the name-keyed tables carrying it, under another {@code collection_id}, until P4.1 drops the name columns ({@link LabelReserved}). Raised before
 * anything is written; {@link dev.nexus.service.http.HttpUtil#sendTypedDbError} answers a typed 409 {@code label_reserved}.
 */
public final class LabelReservedException extends RuntimeException {

    private final String label;

    public LabelReservedException(String label) {
        super("the label '" + label + "' is reserved: a collection renamed away from it still has rows that carry it, and it can be used again "
            + "once P4.1 drops the collection name columns (RDR-229)");
        this.label = label;
    }

    /** The reserved label. */
    public String label() {
        return label;
    }
}
