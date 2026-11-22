// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * The bind-address rules {@link NexusService} applies at boot, moved out of it (RDR-229 P3.5a, nexus-p2m1x.23) to keep the
 * service under its file-size ceiling. {@code NexusService.resolveBindHost} and {@code verifyTenantResetRouteBind} delegate
 * here, so their callers and tests are unchanged.
 */
final class EngineBind {

    private static final Logger log = LoggerFactory.getLogger(EngineBind.class);

    private EngineBind() {
    }

    /** Pure resolution: {@code null}/blank is loopback; otherwise the trimmed value, with a loud warning for a non-loopback bind. */
    static String resolveBindHost(String envValue) {
        if (envValue == null || envValue.isBlank()) {
            return "127.0.0.1";
        }
        String bind = envValue.trim();
        // Normalize "localhost" to "127.0.0.1": InetSocketAddress resolves the name at bind time, and on an IPv6-only-loopback
        // /etc/hosts it would bind [::1] while the supervisor and the Python clients connect to 127.0.0.1 and are refused
        // (code-review H-1). An explicit "::1" is left as it is (deliberate IPv6).
        if (bind.equals("localhost")) {
            return "127.0.0.1";
        }
        if (!isLoopback(bind)) {
            log.warn("event=service_bind_non_loopback bind={} security=\"no external TLS; "
                    + "token-authed plaintext (and unauthenticated /health, /version) "
                    + "exposed beyond loopback — intended only for trusted container "
                    + "networking\"", bind);
        }
        return bind;
    }

    /**
     * Boot refusal (bead nexus-0bl77): the test-only tenant reset route is never allowed on an engine reachable beyond loopback.
     *
     * @throws IllegalStateException when the flag is on and {@code NX_SERVICE_BIND} resolves to anything but {@code 127.0.0.1} or {@code ::1}
     */
    static void verifyTenantResetRouteBind(java.util.function.Function<String, String> env) {
        if (!NexusService.tenantResetRouteEnabled(env)) {
            return;
        }
        String bind = resolveBindHost(env.apply("NX_SERVICE_BIND"));
        if (!isLoopback(bind)) {
            throw new IllegalStateException(NexusService.TENANT_RESET_ROUTE_ENV + "=1 refuses a non-loopback bind (NX_SERVICE_BIND="
                + bind + "): the test-only tenant reset route must not be reachable beyond this host");
        }
    }

    private static boolean isLoopback(String bind) {
        return bind.equals("127.0.0.1") || bind.equals("::1");
    }
}
