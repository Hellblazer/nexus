// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * A write would give one IDENTITY to two different ADDRESSES (nexus-0ehwe arbiter
 * class).
 *
 * <p><strong>Why this is a refusal and not a convergence.</strong> Every table using
 * this exception separates an ADDRESS key from one or more IDENTITY keys:
 *
 * <ul>
 *   <li>{@code catalog_owners} — address {@code (tenant_id, tumbler_prefix)};
 *       identity {@code (tenant_id, name, owner_type)}; alias
 *       {@code (tenant_id, repo_hash)}.</li>
 *   <li>{@code catalog_documents} — address {@code (tenant_id, tumbler)}; identity
 *       {@code (tenant_id, source_uri)} among LIVE rows.</li>
 *   <li>{@code nexus.topics} (nexus-q2ign, {@link TaxonomyRepository}) — address
 *       {@code id} (caller-supplied on the fidelity-import path); identity
 *       {@code (tenant_id, collection, label)} among ROOT topics
 *       ({@code parent_id IS NULL}).</li>
 * </ul>
 *
 * <p>When the caller supplies the address and the identity already lives at a
 * DIFFERENT address, there is no convergent answer. Silently re-targeting the write to
 * the existing address would MISROUTE it (Hal's nexus-jq53b constraint 1: rename must
 * never route through the ensure path); silently updating the addressed row would MERGE
 * two distinct entities — the exact silent-merge class nexus-v6za0 was filed for in this
 * same file. So the operation is refused, loudly and by name.
 *
 * <p>This is still a strict improvement over the pre-fix behaviour, which was a raw
 * unhandled {@code 23505} surfacing as a bare {@code HTTP 409 integrity constraint
 * violation} with no indication of WHICH key fired or WHAT already holds it.
 */
public class CatalogIdentityConflictException extends RuntimeException {

    private final String constraint;
    private final String identity;
    private final String existingAddress;
    private final String attemptedAddress;

    public CatalogIdentityConflictException(String constraint, String identity,
                                            String existingAddress, String attemptedAddress) {
        super(identity + " is already held by " + existingAddress
              + "; refusing to also give it to " + attemptedAddress
              + " (unique key " + constraint + ")");
        this.constraint = constraint;
        this.identity = identity;
        this.existingAddress = existingAddress;
        this.attemptedAddress = attemptedAddress;
    }

    /** The unique constraint or index whose rule this write would break. */
    public String constraint() {
        return constraint;
    }

    /** Human-readable rendering of the identity values that collided. */
    public String identity() {
        return identity;
    }

    /** The address that already holds {@link #identity()}. */
    public String existingAddress() {
        return existingAddress;
    }

    /** The address the refused write was aimed at. */
    public String attemptedAddress() {
        return attemptedAddress;
    }
}
