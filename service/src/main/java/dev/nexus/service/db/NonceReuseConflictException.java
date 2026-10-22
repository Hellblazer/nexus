// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * Typed error (bead nexus-fzteh, shakeout F9): {@code out} on a {@code keys+nonce}
 * template named a tuple id that already exists, but the new write's body or
 * dimensions differ from the stored row's. A nonce names ONE write; a byte-identical
 * retry still lands on the same tuple and returns its id, but a changed payload under
 * the same nonce is a caller bug or a nonce collision, and answering it with the old
 * id would drop the new payload without a word.
 *
 * <p>HTTP 409: unlike {@link MaxLiveRowsExceededException}'s capacity ceiling, retrying
 * the same request can never succeed; the caller must change it (mint a fresh nonce
 * for a new message, or resend the original body). The RDR-222 request ledger refuses
 * an Idempotency-Key reused with a different body the same way.
 *
 * <p>Never raised for a row that was already consumed: an acked row has had its body
 * cleared, so a late retry of the write that row came from is the same write, not a
 * different one.
 */
public final class NonceReuseConflictException extends TupleException {

    private final String id;

    public NonceReuseConflictException(String id, String subspace) {
        super("NonceReuseConflict", 409,
                "tuple id " + id + " already exists in subspace '" + subspace
                        + "' with a different body or dimensions; a nonce names one write, so "
                        + "mint a fresh nonce for a new message, or resend the original body to retry");
        this.id = id;
    }

    /** The existing tuple's id, lowercase hex. */
    public String id() {
        return id;
    }
}
