// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * RDR-205 typed error: {@code ack}/{@code nack} against a {@code claim_id}
 * with no LIVE claim — never claimed, already acked, or already released.
 */
public final class ClaimNotFoundException extends TupleException {

    private final String claimId;

    public ClaimNotFoundException(String claimId) {
        super("ClaimNotFound", 404, "no live claim with id '" + claimId + "'");
        this.claimId = claimId;
    }

    public String claimId() {
        return claimId;
    }
}
