// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.resolver;

import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-169 G3 (bead nexus-064jj) — unit tests for {@link ChromaSchemeHandler}.
 *
 * <p>Uses a lambda stub for {@link ChunkTextFetcher} so there is no DB/Docker
 * dependency and no need to subclass the final {@code PgVectorRepository}.
 *
 * <p>Cases:
 * <ol>
 *   <li>fetchChunkText returns text → ResolveResult.ok with that text.</li>
 *   <li>fetchChunkText returns null → ResolveResult.error("reference_only", ...) — NOT NPE.</li>
 *   <li>Malformed/empty-chash chroma URI → error (reason = "unreachable").</li>
 *   <li>Double-slash URI → "malformed" error (distinct from "reference_only").</li>
 *   <li>Missing collection segment → "unreachable" error.</li>
 *   <li>Phase B forward-compat: errorReason "reference_only" is the branch token.</li>
 *   <li>Null fetcher in constructor → IllegalArgumentException.</li>
 * </ol>
 */
class ChromaSchemeHandlerTest {

    // ── recording fetcher ─────────────────────────────────────────────────

    /**
     * Stub {@link ChunkTextFetcher} that records every call and returns a fixed result.
     * Lambda-based — no subclassing of the final PgVectorRepository.
     */
    private static final class RecordingFetcher implements ChunkTextFetcher {
        final List<String> tenants     = new ArrayList<>();
        final List<String> collections = new ArrayList<>();
        final List<String> chashes     = new ArrayList<>();
        private final String returnText;

        RecordingFetcher(String returnText) { this.returnText = returnText; }

        @Override
        public String fetch(String tenant, String collection, String chash) {
            tenants.add(tenant);
            collections.add(collection);
            chashes.add(chash);
            return returnText;
        }
    }

    // ── happy path ────────────────────────────────────────────────────────

    @Test
    void fetcherReturnsText_yieldsOkResult() {
        RecordingFetcher stub = new RecordingFetcher("the chunk content");
        ChromaSchemeHandler h = new ChromaSchemeHandler(stub);

        String uri    = "chroma://knowledge__nexus__bge-base-en-v15-768__v1/abc123ef";
        ResolveResult result = h.resolve(uri, "tenant-a");

        assertThat(result.isOk()).isTrue();
        assertThat(result.text()).isEqualTo("the chunk content");
        assertThat(result.sourceUri()).isEqualTo(uri);

        // Stub received exactly one call with the right args.
        assertThat(stub.tenants).containsExactly("tenant-a");
        assertThat(stub.collections)
                .containsExactly("knowledge__nexus__bge-base-en-v15-768__v1");
        assertThat(stub.chashes).containsExactly("abc123ef");
    }

    // ── reference-only null ───────────────────────────────────────────────

    @Test
    void fetcherReturnsNull_yieldsReferenceOnlyError_notNpe() {
        // Phase B branch token: "reference_only" → serve address, resolve client-side.
        RecordingFetcher stub = new RecordingFetcher(null);
        ChromaSchemeHandler h = new ChromaSchemeHandler(stub);

        ResolveResult result = h.resolve(
                "chroma://knowledge__nexus__bge-base-en-v15-768__v1/deadbeef", "tenant-b");

        assertThat(result.isOk()).isFalse();
        assertThat(result.errorReason())
                .as("Phase B MUST branch on this exact reason token")
                .isEqualTo("reference_only");
        // L5 fix: errorDetail must be non-null and safe to call .contains() on.
        assertThat(result.errorDetail()).isNotNull();
        assertThat(result.errorDetail()).contains("no stored text");
    }

    // ── malformed URIs ────────────────────────────────────────────────────

    @Test
    void missingChash_yields_unreachableError_noFetcherCall() {
        RecordingFetcher stub = new RecordingFetcher("unused");
        ChromaSchemeHandler h = new ChromaSchemeHandler(stub);

        // URI has collection but empty path segment → empty chash after strip.
        ResolveResult result = h.resolve("chroma://knowledge__nexus__bge-768__v1/", "t");

        assertThat(result.isOk()).isFalse();
        assertThat(result.errorReason()).isEqualTo("unreachable");
        // Fetcher must NOT have been called (fail before the lookup).
        assertThat(stub.chashes).isEmpty();
    }

    @Test
    void missingCollection_yields_unreachableError() {
        RecordingFetcher stub = new RecordingFetcher("unused");
        ChromaSchemeHandler h = new ChromaSchemeHandler(stub);

        // No collection in authority.
        ResolveResult result = h.resolve("chroma:///just-a-chash", "t");

        assertThat(result.isOk()).isFalse();
        assertThat(result.errorReason()).isEqualTo("unreachable");
        assertThat(stub.chashes).isEmpty();
    }

    @Test
    void doubleSlashChash_yields_malformedError_distinctFromReferenceOnly() {
        // chroma://coll//x → chash="/x" after leading-slash strip — must be
        // "malformed", NOT "reference_only", so Phase B can distinguish them.
        RecordingFetcher stub = new RecordingFetcher("unused");
        ChromaSchemeHandler h = new ChromaSchemeHandler(stub);

        ResolveResult result = h.resolve(
                "chroma://knowledge__nexus__bge-768__v1//extra-slash", "t");

        assertThat(result.isOk()).isFalse();
        assertThat(result.errorReason())
                .as("double-slash chash must be 'malformed', not 'reference_only'")
                .isEqualTo("malformed");
        assertThat(stub.chashes).isEmpty();
    }

    // ── construction guard ────────────────────────────────────────────────

    @Test
    void nullFetcher_throws_onConstruction() {
        assertThatThrownBy(() -> new ChromaSchemeHandler((ChunkTextFetcher) null))
                .isInstanceOf(IllegalArgumentException.class);
    }
}
