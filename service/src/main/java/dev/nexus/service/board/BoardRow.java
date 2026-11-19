// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import java.time.Instant;
import java.util.Map;

/**
 * One row of a {@code board/ci/<base>--push}, {@code --done} or {@code --owner} subspace as the owner decision reads
 * it: the engine's own creation time, the dims, the body text and the {@code sender_session} stamp. Rows are plain
 * data so {@link OwnerDecision} stays a pure function of them.
 */
public record BoardRow(String id, Instant createdAt, Map<String, String> dims, String body, String senderSession) {

    public String dim(String name) {
        return dims == null ? null : dims.get(name);
    }

    public String kind() {
        return dim("kind");
    }

    public String sha() {
        return dim("sha");
    }
}
