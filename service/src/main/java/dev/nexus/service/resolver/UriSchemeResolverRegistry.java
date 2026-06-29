// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.resolver;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.net.URI;
import java.net.URISyntaxException;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Global registry mapping URI scheme → {@link UriSchemeHandler}.
 *
 * <h2>RDR-169 G3 — Split by physical reachability</h2>
 *
 * <p>In the multi-tenant cloud service the nexus-engine Java process runs
 * REMOTELY; a tenant's local files ({@code file://}, {@code obsidian://},
 * {@code x-devonthink-item://}, {@code nx-scratch://}) live on the tenant's
 * macOS machine.  This registry is therefore SCOPED to SERVER-REACHABLE
 * schemes only:
 *
 * <ul>
 *   <li>{@code chroma://} — pgvector chunk reassembly (reads
 *       {@code nexus.chunks_<dim>} via the injected PgVectorRepository).</li>
 *   <li>{@code https://} — HTTP fetch for external documents.</li>
 * </ul>
 *
 * LOCAL schemes are handled by the Python bridge
 * ({@code nexus.aspect_readers._READERS}).  The /v1 reference-only response
 * returns the address ({@code collection + chash + source_uri + span});
 * a server-reachable scheme MAY be resolved inline at serving time; a local
 * scheme is resolved client-side by the Python bridge.
 *
 * <h2>Tenant isolation</h2>
 *
 * <p>The registry is a global {@code scheme → handler} map.  Tenant isolation
 * is the handler's responsibility: every handler MUST execute within a
 * {@link dev.nexus.service.db.TenantScope#withTenant} transaction (or an
 * equivalent that stamps {@code nexus.tenant} via
 * {@code SELECT set_config('nexus.tenant', ?, true)}).  Cross-tenant
 * resolution is prevented by RLS FORCE-applied at the Postgres layer;
 * a handler that bypasses {@link dev.nexus.service.db.TenantScope} is a
 * security defect.
 *
 * <h2>Phase A only — un-wired POJO</h2>
 *
 * <p>This bead lands the registry + concrete handlers + unit tests.
 * This class ships un-wired in Phase A: no production code constructs an
 * instance or registers handlers.
 *
 * <p><strong>Phase B (nexus-dtnpu) MUST:</strong>
 * <ol>
 *   <li>Construct a singleton {@code UriSchemeResolverRegistry}.</li>
 *   <li>Register {@link ChromaSchemeHandler} (for {@code chroma://}) and
 *       {@link HttpsSchemeHandler} (for {@code https://}) via
 *       {@link #register(String, UriSchemeHandler)}.</li>
 *   <li>Inject the registry into the /v1 route handler before activation.</li>
 * </ol>
 * Live /v1 reference-only serving route activation is the Phase B boundary.
 */
public final class UriSchemeResolverRegistry {

    private static final Logger log = LoggerFactory.getLogger(UriSchemeResolverRegistry.class);

    /** Scheme → handler map. Schemes are stored in lowercase for consistency. */
    private final Map<String, UriSchemeHandler> handlers = new ConcurrentHashMap<>();

    /**
     * Register a handler for {@code scheme}.
     *
     * <p>Re-registering the same scheme replaces the previous handler (last-write wins).
     * Only server-reachable schemes ({@code chroma}, {@code https}) should be registered
     * here; registering a local scheme ({@code file}, {@code obsidian}, etc.) is not
     * rejected at registration time but will produce a misleading error when the Python
     * bridge dispatches to its own handler for the same URI.
     *
     * @param scheme  the URI scheme to handle (e.g. {@code "chroma"}, {@code "https"});
     *                stored and matched in lowercase
     * @param handler the handler; must not be null
     */
    public void register(String scheme, UriSchemeHandler handler) {
        if (scheme == null || scheme.isBlank()) {
            throw new IllegalArgumentException("scheme must not be null or blank");
        }
        if (handler == null) {
            throw new IllegalArgumentException("handler must not be null");
        }
        handlers.put(scheme.toLowerCase(), handler);
        log.debug("event=resolver_registered scheme={}", scheme.toLowerCase());
    }

    /**
     * Resolve {@code sourceUri} to its text content using the handler for
     * the URI's scheme.
     *
     * @param sourceUri the full URI (e.g. {@code chroma://coll/abc123})
     * @param tenant    the requesting tenant principal; forwarded verbatim to the
     *                  handler so it can stamp the RLS GUC
     * @return non-null {@link ResolveResult}
     * @throws UnknownSchemeException if no handler is registered for the URI's scheme,
     *                                or if the URI has no scheme or is malformed.
     *                                Never returns null; never silently returns an
     *                                empty result for an unrecognised scheme.
     * @throws IllegalArgumentException if {@code sourceUri} or {@code tenant} is blank
     */
    public ResolveResult resolve(String sourceUri, String tenant) {
        if (sourceUri == null || sourceUri.isBlank()) {
            throw new IllegalArgumentException("sourceUri must not be null or blank");
        }
        if (tenant == null || tenant.isBlank()) {
            throw new IllegalArgumentException("tenant must not be null or blank");
        }

        String scheme = extractScheme(sourceUri);
        UriSchemeHandler handler = handlers.get(scheme.toLowerCase());
        if (handler == null) {
            log.warn("event=resolve_unknown_scheme scheme={} uri={}", scheme, sourceUri);
            throw new UnknownSchemeException(scheme, sourceUri);
        }

        log.debug("event=resolve_dispatch scheme={} tenant={}", scheme, tenant);
        return handler.resolve(sourceUri, tenant);
    }

    /**
     * Extract the scheme from a URI string.
     *
     * <p>Uses {@link java.net.URI} for correctness; falls back to a colon-split
     * on parse failure.  Returns an empty string if no scheme can be determined
     * (which will then fail the handler lookup with {@link UnknownSchemeException}).
     */
    private static String extractScheme(String uri) {
        try {
            URI parsed = new URI(uri);
            String s = parsed.getScheme();
            return s != null ? s : "";
        } catch (URISyntaxException e) {
            // Best-effort: take everything before the first colon.
            int colon = uri.indexOf(':');
            return colon > 0 ? uri.substring(0, colon) : "";
        }
    }
}
