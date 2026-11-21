// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

/**
 * The identity the owner decision would count an ack for, from the ack row's {@code sender_session} stamp (RDR-252,
 * nexus-igea9.4): what {@code TupleHandler} stamps into the {@code from} dim of an ack to a {@code --owner} topic, so
 * the label {@code nx group ack-times} reads is the identity the deadline timer counts. Null when the stamp maps to
 * no identity.
 */
@FunctionalInterface
public interface AckIdentity {

    String of(String tenant, String senderSession);

    /** Maps nothing: an ack's {@code from} is left as sent. */
    AckIdentity NONE = (tenant, senderSession) -> null;

    /** The engine's own mapping: the identity {@link Identities#countedIdentity} counts the stamp for. */
    static AckIdentity counting(dev.nexus.service.db.IdentityRepository identities) {
        return (tenant, senderSession) -> Identities.countedIdentity(identities, tenant, senderSession);
    }
}
