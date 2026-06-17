// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import java.net.InetSocketAddress;
import java.net.Proxy;
import java.net.ProxySelector;
import java.net.URI;
import java.util.List;
import java.util.Optional;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.catchThrowable;

/**
 * nexus-f1syh — the Voyage HTTP clients must route through the egress proxy when env
 * configures it (cloud squid; private subnet has no NAT), and connect directly when
 * not. java.net.http.HttpClient ignores https.proxyHost system properties unless a
 * proxy is set explicitly, so this resolver is the load-bearing piece.
 *
 * <p>Tests the pure resolver {@link EgressProxy#fromEnv} (no real env mutation).
 */
class EgressProxyTest {

    private static InetSocketAddress addrOf(Optional<ProxySelector> sel) {
        assertThat(sel).isPresent();
        List<Proxy> proxies = sel.get().select(URI.create("https://api.voyageai.com/v1/embeddings"));
        assertThat(proxies).hasSize(1);
        assertThat(proxies.get(0).type()).isEqualTo(Proxy.Type.HTTP);
        return (InetSocketAddress) proxies.get(0).address();
    }

    @Test
    void nxHttpsProxy_hostPort_resolves() {
        InetSocketAddress a = addrOf(EgressProxy.fromEnv("10.0.1.5:3128", null));
        assertThat(a.getHostString()).isEqualTo("10.0.1.5");
        assertThat(a.getPort()).isEqualTo(3128);
    }

    @Test
    void httpsProxy_url_resolves() {
        InetSocketAddress a = addrOf(EgressProxy.fromEnv(null, "http://squid.internal:3128"));
        assertThat(a.getHostString()).isEqualTo("squid.internal");
        assertThat(a.getPort()).isEqualTo(3128);
    }

    @Test
    void httpsProxy_url_withoutScheme_resolves() {
        InetSocketAddress a = addrOf(EgressProxy.fromEnv(null, "squid.internal:8080"));
        assertThat(a.getHostString()).isEqualTo("squid.internal");
        assertThat(a.getPort()).isEqualTo(8080);
    }

    @Test
    void httpsProxy_url_withoutPort_defaultsToSquid3128() {
        InetSocketAddress a = addrOf(EgressProxy.fromEnv(null, "http://squid.internal"));
        assertThat(a.getPort()).isEqualTo(3128);
    }

    @Test
    void nxHttpsProxy_winsOverHttpsProxy() {
        InetSocketAddress a = addrOf(EgressProxy.fromEnv("10.0.1.5:3128", "http://other:9999"));
        assertThat(a.getHostString()).isEqualTo("10.0.1.5");
        assertThat(a.getPort()).isEqualTo(3128);
    }

    @Test
    void neitherSet_isDirect_empty() {
        // Absent (or blank) → direct connection (local mode / NAT). Not an error.
        assertThat(EgressProxy.fromEnv(null, null)).isEmpty();
        assertThat(EgressProxy.fromEnv("", "  ")).isEmpty();
    }

    @Test
    void presentButMalformed_failsLoud_notSilentDirect() {
        // A proxy var that is SET but unparseable is FATAL — silently direct-connecting
        // would reproduce the original api.voyageai.com timeout opaquely.
        assertThat(catchThrowable(() -> EgressProxy.fromEnv("hostonly", null)))
            .isInstanceOf(IllegalStateException.class).hasMessageContaining("host:port");
        assertThat(catchThrowable(() -> EgressProxy.fromEnv("host:notaport", null)))
            .isInstanceOf(IllegalStateException.class);
        assertThat(catchThrowable(() -> EgressProxy.fromEnv("host:", null)))
            .isInstanceOf(IllegalStateException.class);
        assertThat(catchThrowable(() -> EgressProxy.fromEnv(null, "::::not a url")))
            .isInstanceOf(IllegalStateException.class);
    }
}
