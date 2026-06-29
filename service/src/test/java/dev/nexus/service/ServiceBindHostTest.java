// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-03bcg / ddvjy: NX_SERVICE_BIND host override. Default is loopback;
 * NX_SERVICE_BIND lets a container-hosting deployment bind a reachable address
 * (e.g. 0.0.0.0). The resolution is pure + testable; the security warning for a
 * non-loopback bind is a side effect verified by inspection (loud log).
 */
final class ServiceBindHostTest {

    @Test
    void nullEnvDefaultsToLoopback() {
        assertThat(NexusService.resolveBindHost(null)).isEqualTo("127.0.0.1");
    }

    @Test
    void blankEnvDefaultsToLoopback() {
        assertThat(NexusService.resolveBindHost("")).isEqualTo("127.0.0.1");
        assertThat(NexusService.resolveBindHost("   ")).isEqualTo("127.0.0.1");
    }

    @Test
    void explicitLoopbackIsHonoured() {
        assertThat(NexusService.resolveBindHost("127.0.0.1")).isEqualTo("127.0.0.1");
        assertThat(NexusService.resolveBindHost("localhost")).isEqualTo("localhost");
    }

    @Test
    void nonLoopbackBindIsHonouredAndTrimmed() {
        // The container-hosting opt-in: bind all interfaces so a peer container
        // can reach the service across the network namespace.
        assertThat(NexusService.resolveBindHost("0.0.0.0")).isEqualTo("0.0.0.0");
        assertThat(NexusService.resolveBindHost("  0.0.0.0  ")).isEqualTo("0.0.0.0");
    }
}
