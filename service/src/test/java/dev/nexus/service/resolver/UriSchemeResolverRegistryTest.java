// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.resolver;

import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-169 G3 (bead nexus-064jj) — Java half of the URI-scheme resolver
 * registry.
 *
 * <p>Scope: SERVER-REACHABLE schemes only ({@code chroma://},
 * {@code https://}).  LOCAL schemes ({@code file://},
 * {@code obsidian://}, {@code x-devonthink-item://},
 * {@code nx-scratch://}) are handled by the Python bridge in
 * {@code nexus.aspect_readers}. The boundary is documented in
 * {@link UriSchemeResolverRegistry}'s class javadoc.
 *
 * <p>Cases asserted:
 * <ol>
 *   <li>Dispatch by scheme — each registered handler is invoked for its scheme.</li>
 *   <li>Handler receives tenant context verbatim.</li>
 *   <li>Unknown scheme fails loud ({@link UnknownSchemeException}), never silent null.</li>
 *   <li>Two stub handlers registered: correct one dispatched per scheme.</li>
 *   <li>Stub chroma handler delegates to the registry's resolution path.</li>
 *   <li>Stub https handler delegates to the registry's resolution path.</li>
 * </ol>
 */
class UriSchemeResolverRegistryTest {

    // ── stub handlers ──────────────────────────────────────────────────────

    /** Records every (uri, tenant) it was called with. */
    private static final class RecordingHandler implements UriSchemeHandler {
        final List<String> capturedUris    = new ArrayList<>();
        final List<String> capturedTenants = new ArrayList<>();
        private final String returnText;

        RecordingHandler(String returnText) {
            this.returnText = returnText;
        }

        @Override
        public ResolveResult resolve(String uri, String tenant) {
            capturedUris.add(uri);
            capturedTenants.add(tenant);
            return ResolveResult.ok(returnText, uri);
        }
    }

    // ── dispatch tests ─────────────────────────────────────────────────────

    @Test
    void dispatch_chromaScheme_invokesChromaHandler() {
        RecordingHandler chromaH = new RecordingHandler("chunk text from pgvector");
        RecordingHandler httpsH  = new RecordingHandler("web content");

        UriSchemeResolverRegistry registry = new UriSchemeResolverRegistry();
        registry.register("chroma", chromaH);
        registry.register("https",  httpsH);

        ResolveResult result = registry.resolve(
                "chroma://knowledge__nexus__bge-base-en-v15-768__v1/abc123", "tenant-a");

        assertThat(result.isOk()).isTrue();
        assertThat(result.text()).isEqualTo("chunk text from pgvector");
        // chroma handler was called; https was not
        assertThat(chromaH.capturedUris).hasSize(1)
                .containsExactly("chroma://knowledge__nexus__bge-base-en-v15-768__v1/abc123");
        assertThat(httpsH.capturedUris).isEmpty();
    }

    @Test
    void dispatch_httpsScheme_invokesHttpsHandler() {
        RecordingHandler chromaH = new RecordingHandler("pgvector text");
        RecordingHandler httpsH  = new RecordingHandler("fetched html");

        UriSchemeResolverRegistry registry = new UriSchemeResolverRegistry();
        registry.register("chroma", chromaH);
        registry.register("https",  httpsH);

        ResolveResult result = registry.resolve(
                "https://example.com/doc", "tenant-b");

        assertThat(result.isOk()).isTrue();
        assertThat(result.text()).isEqualTo("fetched html");
        assertThat(httpsH.capturedUris).hasSize(1)
                .containsExactly("https://example.com/doc");
        assertThat(chromaH.capturedUris).isEmpty();
    }

    @Test
    void handler_receives_tenantContext_verbatim() {
        RecordingHandler handler = new RecordingHandler("text");

        UriSchemeResolverRegistry registry = new UriSchemeResolverRegistry();
        registry.register("chroma", handler);

        registry.resolve("chroma://coll/abc", "tenant-xyz");

        assertThat(handler.capturedTenants).containsExactly("tenant-xyz");
    }

    @Test
    void unknownScheme_failsLoud_neverSilentNull() {
        UriSchemeResolverRegistry registry = new UriSchemeResolverRegistry();
        registry.register("chroma", new RecordingHandler("text"));

        // A local scheme that belongs to the Python bridge must not be
        // silently handled or return null — it must throw UnknownSchemeException.
        assertThatThrownBy(() -> registry.resolve("file:///some/path", "t"))
                .isInstanceOf(UnknownSchemeException.class)
                .hasMessageContaining("file");

        assertThatThrownBy(() -> registry.resolve("obsidian://open?file=x", "t"))
                .isInstanceOf(UnknownSchemeException.class)
                .hasMessageContaining("obsidian");
    }

    @Test
    void emptyScheme_failsLoud() {
        UriSchemeResolverRegistry registry = new UriSchemeResolverRegistry();

        assertThatThrownBy(() -> registry.resolve("no-colon-here", "t"))
                .isInstanceOf(UnknownSchemeException.class);
    }

    @Test
    void twoStubHandlers_correctDispatch_bothRegistered() {
        RecordingHandler aHandler = new RecordingHandler("a result");
        RecordingHandler bHandler = new RecordingHandler("b result");

        UriSchemeResolverRegistry registry = new UriSchemeResolverRegistry();
        registry.register("scheme-a", aHandler);
        registry.register("scheme-b", bHandler);

        ResolveResult ra = registry.resolve("scheme-a://host/path", "tenant-1");
        ResolveResult rb = registry.resolve("scheme-b://host/path", "tenant-2");

        assertThat(ra.text()).isEqualTo("a result");
        assertThat(rb.text()).isEqualTo("b result");
        // cross-dispatch: a was not called for scheme-b and vice-versa
        assertThat(aHandler.capturedTenants).containsExactly("tenant-1");
        assertThat(bHandler.capturedTenants).containsExactly("tenant-2");
    }

    @Test
    void resolveResult_error_isNotOk() {
        ResolveResult err = ResolveResult.error("scheme_unknown", "no handler for xyz");
        assertThat(err.isOk()).isFalse();
        assertThat(err.text()).isNull();
        assertThat(err.errorReason()).isEqualTo("scheme_unknown");
    }

    @Test
    void resolveResult_ok_isOk() {
        ResolveResult ok = ResolveResult.ok("content text", "chroma://coll/abc");
        assertThat(ok.isOk()).isTrue();
        assertThat(ok.text()).isEqualTo("content text");
        assertThat(ok.errorReason()).isNull();
    }
}
