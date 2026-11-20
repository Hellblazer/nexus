// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import java.util.Map;

/**
 * What the decision needs to know about identities, read once under the topic lock: which identity each
 * {@code sender_session} stamp maps to (a delegated identity already mapped to its parent) and each identity's
 * state and lease with the 300-second rule applied. An unmapped stamp, or an identity with no entry, is unknown.
 */
public record Identities(Map<String, String> identityOfStamp, Map<String, Presence> presence) {

    /**
     * An identity's state ({@code live}, {@code lapsed}, {@code unheld}, {@code ended}), lease and kind
     * ({@code seat}, {@code ephemeral}, {@code delegated}).
     */
    public record Presence(String state, String lease, String kind) {
    }

    /**
     * The identity the owner decision counts a {@code sender_session} stamp for, read through the same rule as
     * {@link BoardReads#identities}: the session index maps the stamp to an identity, and a delegated identity is
     * counted as its parent. Null when the stamp is absent or the index does not map it. One read each of the
     * session index and the identity's liveness, outside any lock.
     */
    public static String countedIdentity(dev.nexus.service.db.IdentityRepository identities, String tenant, String stamp) {
        if (stamp == null || stamp.isBlank()) {
            return null;
        }
        var mapped = identities.sessionIdentity(tenant, stamp);
        if (mapped.isEmpty()) {
            return null;
        }
        String id = mapped.get().identity();
        return parentIfDelegated(id, identities.liveness(tenant, id).orElse(null));
    }

    /** The identity an id is counted as: its parent when it is a delegated identity with one, else itself. */
    static String parentIfDelegated(String id, dev.nexus.service.db.IdentityRepository.Liveness l) {
        return l != null && "delegated".equals(l.kind()) && l.parent() != null ? l.parent() : id;
    }

    public static final String LIVE = "live";
    public static final String UNLEASED = "unleased";

    /** The identity a stamp maps to, or null when the stamp is absent or the session index does not map it. */
    public String identityOf(String stamp) {
        return stamp == null || stamp.isBlank() ? null : identityOfStamp.get(stamp);
    }

    /** Live means the read says {@code live}; an {@code unleased} identity is not live. */
    public boolean live(String identity) {
        Presence p = identity == null ? null : presence.get(identity);
        return p != null && LIVE.equals(p.state()) && !UNLEASED.equals(p.lease());
    }

    /** A live, leased seat: the only identity a next-up candidate taken from a roster may be. */
    public boolean liveSeat(String identity) {
        Presence p = identity == null ? null : presence.get(identity);
        return live(identity) && "seat".equals(p.kind());
    }

    public boolean unleased(String identity) {
        Presence p = identity == null ? null : presence.get(identity);
        return p != null && LIVE.equals(p.state()) && UNLEASED.equals(p.lease());
    }
}
