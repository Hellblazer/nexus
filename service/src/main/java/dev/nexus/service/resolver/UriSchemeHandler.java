// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.resolver;

/**
 * Handler for a single URI scheme in the server-side resolver registry.
 *
 * <p>RDR-169 G3 split: this interface covers SERVER-REACHABLE schemes only
 * ({@code chroma://} for pgvector chunk reassembly, {@code https://} for
 * HTTP fetch).  LOCAL schemes ({@code file://}, {@code obsidian://},
 * {@code x-devonthink-item://}, {@code nx-scratch://}) are handled by the
 * Python bridge in {@code nexus.aspect_readers} and must not be registered
 * here — see {@link UriSchemeResolverRegistry}.
 *
 * <p>Tenant context is threaded via {@code current_setting('nexus.tenant')}
 * (the RLS GUC pattern from {@link dev.nexus.service.db.TenantScope}) so
 * cross-tenant data never leaks: the handler MUST only access rows whose
 * {@code tenant_id} equals the stamped tenant.
 */
public interface UriSchemeHandler {

    /**
     * Resolve {@code uri} and return its text content.
     *
     * @param uri    the full URI to resolve (scheme already verified by the registry)
     * @param tenant the requesting tenant principal (never null or blank when wired
     *               through {@link UriSchemeResolverRegistry#resolve})
     * @return a non-null {@link ResolveResult}; never {@code null}
     */
    ResolveResult resolve(String uri, String tenant);
}
