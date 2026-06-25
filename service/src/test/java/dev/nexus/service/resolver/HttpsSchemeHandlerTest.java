// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.resolver;

import org.junit.jupiter.api.Test;

import java.net.http.HttpClient;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-169 G3 (bead nexus-064jj) — unit tests for {@link HttpsSchemeHandler}.
 *
 * <p>Tests that do not need a live network connection exercise the guard paths
 * directly (scheme guard, null guard, construction guard).  The
 * {@code AutoCloseable} contract is checked via the handler's {@code close()}
 * method.  Live-network tests are omitted — they belong in integration tests
 * tagged {@code @Tag("integration")}.
 *
 * <p>Cases:
 * <ol>
 *   <li>Non-https URI → error (scheme guard), not NPE/exception.</li>
 *   <li>Null URI → error, not NPE.</li>
 *   <li>Handler implements {@link AutoCloseable} — close() does not throw.</li>
 *   <li>Null HttpClient in constructor → {@link IllegalArgumentException}.</li>
 * </ol>
 */
class HttpsSchemeHandlerTest {

    @Test
    void nonHttpsUri_yields_unreachableError_schemeGuard() {
        // The scheme guard fires before request construction — no network call.
        // Use the default (real) HttpClient; it is never invoked.
        HttpsSchemeHandler h = new HttpsSchemeHandler();

        ResolveResult result = h.resolve("http://example.com/doc", "tenant-a");

        assertThat(result.isOk()).isFalse();
        assertThat(result.errorReason()).isEqualTo("unreachable");
        assertThat(result.errorDetail()).contains("non-https");

        h.close();
    }

    @Test
    void nullUri_yields_unreachableError_notNpe() {
        HttpsSchemeHandler h = new HttpsSchemeHandler();

        ResolveResult result = h.resolve(null, "tenant-b");

        assertThat(result.isOk()).isFalse();
        assertThat(result.errorReason()).isEqualTo("unreachable");

        h.close();
    }

    @Test
    void handler_implements_autoCloseable_closeDoesNotThrow() {
        HttpsSchemeHandler h = new HttpsSchemeHandler();

        assertThat(h).isInstanceOf(AutoCloseable.class);
        // Must not throw.
        h.close();
    }

    @Test
    void nullHttpClient_throws_onConstruction() {
        assertThatThrownBy(() -> new HttpsSchemeHandler((HttpClient) null))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void fileScheme_injectedClient_yields_unreachableError_schemeGuard() {
        // Any non-https scheme must be rejected before touching the client.
        HttpsSchemeHandler h = new HttpsSchemeHandler();

        ResolveResult result = h.resolve("file:///some/path", "tenant-c");

        assertThat(result.isOk()).isFalse();
        assertThat(result.errorReason()).isEqualTo("unreachable");

        h.close();
    }
}
