// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * RDR-205 typed error: {@code ack}/{@code nack} against a live claim held
 * by a DIFFERENT claimant.
 */
public final class ClaimOwnershipException extends TupleException {

    private final String claimId;
    private final String claimant;

    public ClaimOwnershipException(String claimId, String claimant) {
        super("ClaimOwnership", 403,
                "claim '" + claimId + "' is not held by claimant '" + claimant + "'");
        this.claimId = claimId;
        this.claimant = claimant;
    }

    public String claimId() {
        return claimId;
    }

    public String claimant() {
        return claimant;
    }
}
