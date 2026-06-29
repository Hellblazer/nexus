// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.resolver;

/**
 * Thrown by {@link UriSchemeResolverRegistry#resolve} when no handler is
 * registered for the URI's scheme.
 *
 * <p>Fail-loud contract (RDR-169 G3): unknown schemes must never return a
 * silent null or degrade quietly.  Callers that expect "not found" as a
 * normal outcome should catch this exception and convert it to their own
 * error representation.
 */
public final class UnknownSchemeException extends RuntimeException {

    private final String scheme;

    public UnknownSchemeException(String scheme, String uri) {
        super("no handler registered for scheme '" + scheme + "' (uri='" + uri + "'). "
              + "LOCAL schemes (file, obsidian, x-devonthink-item, nx-scratch) belong "
              + "to the Python bridge, not this registry.");
        this.scheme = scheme;
    }

    /** The unrecognised scheme token (e.g. {@code "file"}, {@code "obsidian"}). */
    public String scheme() {
        return scheme;
    }
}
