// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import java.util.UUID;
import java.util.regex.Pattern;

/**
 * The request correlation id at the engine (RDR-167 Step 3).
 *
 * <p>An inbound {@code X-Correlation-ID} is kept when the whole value matches
 * {@code [A-Za-z0-9._-]{1,64}}; anything else (absent, blank, too long, a space,
 * a trailing newline) is replaced by an id the engine generates. A generated id
 * is {@code nxe-} followed by 32 lowercase hex characters. The nx client mints
 * 32 hex characters with no prefix, so an nx id never carries the marker. An
 * inbound id that already starts with {@code nxe-} passes the accept rule and is
 * kept like any other, so the marker proves an engine origin only when the
 * sender is known not to use it. The Phase 2 edge probe is such a sender: it
 * sends a valid id P without the prefix and reads four outcomes from the echo
 * (RDR-167 residual 1): equal to P is a pass-through; an {@code nxe-} prefix
 * means the engine received no valid header (stripped, or replaced by an
 * invalid value); any other value is an overwrite; no echo is a response strip.
 *
 * <p>The match is {@link java.util.regex.Matcher#matches()} over the whole
 * value, never {@code find()} with {@code ^...$}: {@code $} also admits a
 * trailing newline.
 */
public final class CorrelationId {

    /** The header the client sends and the engine echoes. */
    public static final String HEADER = "X-Correlation-ID";

    /** The prefix that marks an id the engine generated. */
    public static final String GENERATED_PREFIX = "nxe-";

    private static final Pattern ACCEPT = Pattern.compile("[A-Za-z0-9._-]{1,64}");

    private CorrelationId() {
    }

    /** The inbound id when it passes the accept rule, else a generated one. */
    static String acceptOrGenerate(String inbound) {
        return inbound != null && ACCEPT.matcher(inbound).matches() ? inbound : generate();
    }

    /** A new engine id: {@code nxe-} and 32 lowercase hex characters. */
    static String generate() {
        return GENERATED_PREFIX + UUID.randomUUID().toString().replace("-", "");
    }

    /**
     * True when {@code id} carries the {@code nxe-} marker. That is an engine
     * origin only for a sender that never sends the marker itself.
     */
    static boolean isGenerated(String id) {
        return id != null && id.startsWith(GENERATED_PREFIX);
    }
}
