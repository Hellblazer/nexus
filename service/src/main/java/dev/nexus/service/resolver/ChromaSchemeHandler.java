// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.resolver;

import dev.nexus.service.vectors.PgVectorRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.net.URI;

/**
 * Resolves {@code chroma://} URIs to chunk text via pgvector chunk reassembly.
 *
 * <p>RDR-169 G3 (bead nexus-064jj) — server-reachable scheme.  A
 * {@code chroma://} URI encodes the pgvector {@code collection} and
 * {@code chash} as the authority and path of the URI:
 *
 * <pre>  chroma://&lt;collection&gt;/&lt;chash&gt;</pre>
 *
 * <p>Example: {@code chroma://knowledge__nexus__bge-base-en-v15-768__v1/abc123ef}
 *
 * <p>Resolution fetches the {@code chunk_text} column for the
 * {@code (tenant, collection, chash)} triple from the
 * {@code nexus.chunks_<dim>} table via the injected {@link ChunkTextFetcher}.
 * In production wire as:
 *
 * <pre>  new ChromaSchemeHandler(pgVectorRepository::fetchChunkText)</pre>
 *
 * <p>RLS is enforced by {@link dev.nexus.service.db.TenantScope}: the fetcher
 * stamps {@code nexus.tenant} before touching the table, so cross-tenant rows
 * are invisible.
 *
 * <p>Phase A: the handler is registered and unit-tested here.  Live /v1
 * reference-only serving that calls this handler is Phase B (bead nexus-dtnpu).
 */
public final class ChromaSchemeHandler implements UriSchemeHandler {

    private static final Logger log = LoggerFactory.getLogger(ChromaSchemeHandler.class);

    private final ChunkTextFetcher fetcher;

    /**
     * Primary constructor (production and tests): accepts any {@link ChunkTextFetcher}.
     * In production wire as {@code new ChromaSchemeHandler(pgVector::fetchChunkText)}.
     *
     * @param fetcher chunk-text lookup; must not be null
     */
    public ChromaSchemeHandler(ChunkTextFetcher fetcher) {
        if (fetcher == null) {
            throw new IllegalArgumentException("fetcher must not be null");
        }
        this.fetcher = fetcher;
    }

    /**
     * Convenience constructor for callers that already hold a
     * {@link PgVectorRepository}.
     *
     * @param pgVector repository owning the {@code chunks_<dim>} tables; must not be null
     */
    public ChromaSchemeHandler(PgVectorRepository pgVector) {
        this(pgVector::fetchChunkText);
    }

    /**
     * Resolve a {@code chroma://collection/chash} URI to its stored chunk text.
     *
     * <p>Returns {@link ResolveResult#error} (never throws) when the URI is
     * malformed, the chunk is missing, or {@code chunk_text} is NULL (reference-only
     * chunk — by design, reference-only chunks have no stored text; Phase B must
     * branch on {@code errorReason() == "reference_only"} to distinguish this from
     * a URI error).
     */
    @Override
    public ResolveResult resolve(String uri, String tenant) {
        // Parse: chroma://<collection>/<chash>
        // URI.getHost() returns the authority (collection); getPath() has a leading "/".
        String collection;
        String chash;
        try {
            URI parsed = new URI(uri);
            // getAuthority() instead of getHost(): collection names contain double-underscores
            // (e.g. knowledge__nexus__bge-base-en-v15-768__v1) which are not valid hostname
            // characters per RFC 3986, so getHost() returns null for these.
            collection = parsed.getAuthority();
            String path = parsed.getPath();
            chash = (path != null && path.startsWith("/")) ? path.substring(1) : path;
        } catch (Exception e) {
            return ResolveResult.error("unreachable",
                    "malformed chroma URI '" + uri + "': " + e.getMessage());
        }

        if (collection == null || collection.isBlank()) {
            return ResolveResult.error("unreachable",
                    "chroma URI missing collection (authority): " + uri);
        }
        if (chash == null || chash.isBlank()) {
            return ResolveResult.error("unreachable",
                    "chroma URI missing chash (path): " + uri);
        }
        // Reject a chash that still starts with "/" after the leading-slash strip
        // (indicates double-slash like chroma://coll//x → chash="/x"), or contains
        // internal slashes that suggest the path was not stripped correctly.
        if (chash.startsWith("/") || chash.contains("//")) {
            return ResolveResult.error("malformed",
                    "chroma URI has malformed chash segment '" + chash + "' (double-slash?): " + uri);
        }

        log.debug("event=chroma_resolve tenant={} collection={} chash={}", tenant, collection, chash);

        // Fetch chunk text — stamps the RLS GUC via TenantScope so cross-tenant rows
        // are invisible by construction.
        String text = fetcher.fetch(tenant, collection, chash);
        if (text == null) {
            // NULL chunk_text = reference-only retention (RDR-169 G1) or missing row.
            // Phase B MUST branch on this reason token to decide 404 vs. client-redirect.
            return ResolveResult.error("reference_only",
                    "chunk (" + collection + ", " + chash + ") has no stored text "
                    + "(reference-only retention or missing row) — resolve client-side");
        }

        return ResolveResult.ok(text, uri);
    }
}
