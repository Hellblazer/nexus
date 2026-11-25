// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * A restore into {@code label} named quarantined chunks whose origin tag is the null marker (RDR-229 P3.7, linda 2026-11-23): from gate 3 a quarantined
 * chunk is the origin's by the origin's registry id, and a chunk quarantined from a collection that had no registry row, or no longer has one, carries
 * {@code origin_collection_id: null} and is not restorable by id. The restore functions skip such a row, which would answer an unexplained zero, so the
 * repository refuses first, before anything moves. {@link dev.nexus.service.http.HttpUtil#sendTypedDbError} answers a typed 409
 * {@code restore_origin_unregistered} naming the label and the number of rows. The chunks stay quarantined.
 */
public final class RestoreOriginUnregisteredException extends RuntimeException {

    private final String label;
    private final long rows;

    public RestoreOriginUnregisteredException(String label, long rows) {
        super(rows + " quarantined chunk(s) tagged with the origin '" + label + "' carry no collection id (the origin had no registry row when they were quarantined), "
            + "so they cannot be restored by id; nothing was restored and they stay quarantined");
        this.label = label;
        this.rows = rows;
    }

    /** The origin label the restore named. */
    public String label() {
        return label;
    }

    /** How many quarantined chunks of that label carry the null marker. */
    public long rows() {
        return rows;
    }
}
