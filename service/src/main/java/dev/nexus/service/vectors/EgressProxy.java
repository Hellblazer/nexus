// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.net.InetSocketAddress;
import java.net.ProxySelector;
import java.net.URI;
import java.util.Optional;

/**
 * nexus-... egress-proxy configuration for the Voyage HTTP clients, read from env.
 *
 * <p><b>Why this exists.</b> The cloud deploy routes outbound {@code api.voyageai.com}
 * through an egress proxy (squid) because the private subnet has no NAT/IGW. The IaC
 * set the proxy via {@code JAVA_TOOL_OPTIONS=-Dhttps.proxyHost=…}. On a standard JVM
 * that works: {@code HttpClient.newBuilder().build()} with no {@code .proxy(...)} falls
 * back to {@code ProxySelector.getDefault()} ({@code sun.net.spi.DefaultProxySelector}),
 * which honors {@code https.proxyHost} — so the old JVM engine proxied correctly. A
 * GraalVM NATIVE image breaks this two ways: (1) it ignores {@code JAVA_TOOL_OPTIONS}
 * (a JVM-launcher var), and (2) {@code DefaultProxySelector} is not initialized at
 * native-image build time, so {@code ProxySelector.getDefault()} resolves to DIRECT and
 * the system properties are ignored regardless. Same family as the boot segfault
 * (nexus-0n7uc): the deploy relied on a JVM behavior native-image drops. The robust fix
 * is to set the proxy ON the client explicitly, from env — independent of JVM-launcher
 * vars and of the default-selector's native gap.
 *
 * <p>Env contract (RDR-157 config-via-env):
 * <ul>
 *   <li>{@code NX_HTTPS_PROXY} — {@code host:port} (preferred, explicit; an IPv6
 *       literal must use bracket notation {@code [::1]:3128}, since a bare IPv6
 *       address is ambiguous with the host:port split — the cloud squid is IPv4), or</li>
 *   <li>{@code HTTPS_PROXY} — a URL ({@code http://host:port}; the de-facto standard).</li>
 * </ul>
 * Absent / blank → {@link Optional#empty()} → the client connects directly (local mode,
 * or a cloud deploy with NAT). These clients only ever call {@code api.voyageai.com}
 * (always external), so no {@code NO_PROXY}/nonProxyHosts handling is needed here.
 */
public final class EgressProxy {

    private static final Logger log = LoggerFactory.getLogger(EgressProxy.class);

    private EgressProxy() {
    }

    /** Resolve the egress proxy from the process environment. */
    public static Optional<ProxySelector> selector() {
        return fromEnv(System.getenv("NX_HTTPS_PROXY"), System.getenv("HTTPS_PROXY"));
    }

    /**
     * Pure resolver (testable): {@code NX_HTTPS_PROXY} ({@code host:port}) wins, else
     * the {@code HTTPS_PROXY} URL.
     *
     * <p>Failure posture (no-silent-fallbacks-for-correctness): ABSENT (neither set, or
     * blank) → {@link Optional#empty()} = direct connection (local mode, or a cloud
     * deploy with NAT). But a var that is PRESENT yet unparseable is FATAL —
     * {@link IllegalStateException}. An operator who set the proxy needs it; silently
     * falling back to a direct connection would reproduce the original outage (a 30s
     * timeout per request to api.voyageai.com on a no-NAT subnet) with no clear cause.
     * Fail loud at boot instead.
     *
     * @param nxHostPort   value of {@code NX_HTTPS_PROXY} (host:port), or null/blank
     * @param httpsProxyUrl value of {@code HTTPS_PROXY} (URL), or null/blank
     * @throws IllegalStateException if a present env var cannot be parsed
     */
    static Optional<ProxySelector> fromEnv(String nxHostPort, String httpsProxyUrl) {
        if (nxHostPort != null && !nxHostPort.isBlank()) {
            return Optional.of(parseHostPort(nxHostPort.trim(), "NX_HTTPS_PROXY"));
        }
        if (httpsProxyUrl != null && !httpsProxyUrl.isBlank()) {
            return Optional.of(parseUrl(httpsProxyUrl.trim()));
        }
        return Optional.empty();
    }

    private static ProxySelector parseHostPort(String hostPort, String source) {
        int colon = hostPort.lastIndexOf(':');
        if (colon > 0 && colon < hostPort.length() - 1) {
            try {
                return of(hostPort.substring(0, colon),
                          Integer.parseInt(hostPort.substring(colon + 1)), source);
            } catch (NumberFormatException ignored) {
                // fall through to the fatal error below
            }
        }
        throw fatal(source, hostPort, "expected host:port");
    }

    private static ProxySelector parseUrl(String url) {
        try {
            URI u = URI.create(url.contains("://") ? url : "http://" + url);
            String host = u.getHost();
            if (host != null && !host.isBlank()) {
                int port = u.getPort() != -1 ? u.getPort() : 3128;  // squid default
                return of(host, port, "HTTPS_PROXY");
            }
        } catch (IllegalArgumentException ignored) {
            // fall through to the fatal error below
        }
        throw fatal("HTTPS_PROXY", url, "expected a URL like http://host:port");
    }

    private static IllegalStateException fatal(String source, String value, String hint) {
        // Loud, not silent: a set-but-broken proxy var would otherwise direct-connect
        // and reproduce the api.voyageai.com timeout opaquely.
        return new IllegalStateException(
            source + "=" + value + " is set but unparseable (" + hint + "). The egress "
            + "proxy is required wherever it is configured (direct connection to "
            + "api.voyageai.com on a no-NAT subnet times out); fix the value.");
    }

    private static ProxySelector of(String host, int port, String source) {
        log.info("event=egress_proxy_configured source={} host={} port={}", source, host, port);
        return ProxySelector.of(new InetSocketAddress(host, port));
    }
}
