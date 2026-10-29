// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * nexus-5r2sc.2.5 (RDR-235 E5): a live claim, held by the named claimant, that is not a claim on the aspect
 * task for the document the write names. A claim id is a credential for ONE task, so
 * {@code POST /v1/aspects/upsert} refuses it for another document's write, or for a claim on a tuple outside
 * the {@code aspects} subspace, or on an {@code aspects} tuple whose body is not a JSON object carrying a
 * {@code doc_id}. Rendered 403 {@code ClaimTaskMismatch}; nothing is written.
 */
public final class ClaimTaskMismatchException extends TupleException {

    public ClaimTaskMismatchException(String claimId, String what) {
        super("ClaimTaskMismatch", 403, "claim '" + claimId + "' is not a claim on " + what);
    }
}
