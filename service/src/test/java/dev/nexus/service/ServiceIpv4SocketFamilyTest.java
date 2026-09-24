// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Locale;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * The socket FAMILY, observed where it is actually visible: the kernel.
 *
 * <p>WHY THIS FILE EXISTS. An earlier draft of this work claimed the socket
 * family was untestable before the Phase 4 Windows leg. Both reviewers of
 * nexus-ijue9.7 called that an over-generalisation and they were right:
 * {@code service-ci} runs ubuntu-latest exclusively, this suite already gates
 * Linux-only work with {@link Assumptions} (Bge768ParityTest,
 * RerankStageIntegrationTest), and {@code /proc/net/tcp*} is readable here.
 * The claim was true of the JAVA API and got generalised to "untestable",
 * which is a different and false statement. This test is the instrument that
 * then settled the whole design.
 *
 * <p>WHAT THE JAVA API CANNOT SEE, kept because it is why a weaker test would
 * be worse than none. Measured on linux/amd64:
 * {@code HttpServer.create(new InetSocketAddress("127.0.0.1", 0))} opens an
 * AF_INET6 socket bound to {@code ::ffff:127.0.0.1} — present in
 * {@code /proc/net/tcp6}, absent from {@code /proc/net/tcp} — while
 * {@code server.getAddress().getAddress()} reports {@code Inet4Address} in
 * BOTH configurations. An assertion on the address object passes on the bug.
 *
 * <p>WHAT THIS DOES AND DOES NOT COVER. It exercises the JVM, where the
 * dual-stack default is the defect. The shipped artifact is a GraalVM native
 * image, and there the default is corrected by
 * {@link Ipv4StackFeature} — verified separately by building a native image
 * and reading the same {@code /proc} tables (IPv4 table populated, IPv6
 * empty). This test therefore pins the PREMISE (the JVM default is
 * dual-stack, so the mechanism is still needed); the Feature's effect is
 * proven by that native measurement, not here.
 */
final class ServiceIpv4SocketFamilyTest {

    /** /proc/net/tcp6 encodes the v4-mapped loopback ::ffff:127.0.0.1 as this. */
    private static final String V4_MAPPED_LOOPBACK = "0000000000000000FFFF00000100007F";
    /** /proc/net/tcp encodes 127.0.0.1 as this (little-endian per 32-bit word). */
    private static final String V4_LOOPBACK = "0100007F";

    private static boolean procNetAvailable() {
        return Files.isReadable(Path.of("/proc/net/tcp"))
            && Files.isReadable(Path.of("/proc/net/tcp6"));
    }

    /** Local listening addresses in a /proc/net/tcp* table, upper-cased. */
    private static List<String> listeningLocalAddresses(String table) throws IOException {
        return Files.readAllLines(Path.of("/proc/net/" + table)).stream()
            .skip(1)                                   // header
            .map(String::trim)
            .filter(l -> !l.isEmpty())
            .map(l -> l.split("\\s+"))
            .filter(f -> f.length > 3 && "0A".equals(f[3]))   // st=0A is TCP_LISTEN
            .map(f -> f[1].toUpperCase(Locale.ROOT))
            .toList();
    }

    private static String hexPort(int port) {
        return String.format("%04X", port);
    }

    /**
     * The premise: a plain loopback bind is NOT an IPv4 socket on Linux.
     *
     * <p>This asserts the BUG, deliberately. If the JDK ever changes so that
     * binding 127.0.0.1 yields AF_INET by default, this goes red and the whole
     * NX_SERVICE_IPV4_ONLY mechanism becomes unnecessary — which is a finding,
     * not a failure. A test that only checked the fixed state would leave the
     * mechanism in place forever after its reason expired.
     */
    @Test
    void plainLoopbackBindIsDualStackOnLinux() throws Exception {
        Assumptions.assumeTrue(procNetAvailable(), "needs Linux /proc/net/tcp*");
        Assumptions.assumeTrue(
            !"true".equals(System.getProperty(Ipv4StackFeature.PREFER_IPV4_PROPERTY)),
            "another test pinned preferIPv4Stack in this JVM; this one needs the default");

        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 10);
        server.start();
        try {
            String port = hexPort(server.getAddress().getPort());

            assertThat(listeningLocalAddresses("tcp6"))
                .as("the listener should be an AF_INET6 socket on ::ffff:127.0.0.1 "
                    + "(this is the defect RDR-218 Gap 2 describes)")
                .contains(V4_MAPPED_LOOPBACK + ":" + port);

            assertThat(listeningLocalAddresses("tcp"))
                .as("and correspondingly absent from the IPv4 table")
                .doesNotContain(V4_LOOPBACK + ":" + port);

            // The trap, pinned: the Java API reports IPv4 for this very socket.
            assertThat(server.getAddress().getAddress())
                .as("java.net reports Inet4Address for an AF_INET6 socket, which "
                    + "is why no assertion on this object can detect the defect")
                .isInstanceOf(java.net.Inet4Address.class);
        } finally {
            server.stop(0);
        }
    }

    /**
     * Non-vacuity for the reader of the table, not just the socket.
     *
     * <p>If {@link #listeningLocalAddresses} silently returned empty — a
     * changed /proc format, a wrong column, a filter that matches nothing —
     * the {@code doesNotContain} assertion above would pass for the wrong
     * reason. This proves the parser finds the socket it is looking for.
     */
    @Test
    void theProcReaderActuallyFindsALiveListener() throws Exception {
        Assumptions.assumeTrue(procNetAvailable(), "needs Linux /proc/net/tcp*");

        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 10);
        server.start();
        try {
            String port = hexPort(server.getAddress().getPort());
            List<String> v4 = listeningLocalAddresses("tcp");
            List<String> v6 = listeningLocalAddresses("tcp6");
            assertThat(v4.size() + v6.size())
                .as("the reader parsed no listening sockets at all; every "
                    + "absence assertion in this file would be vacuous")
                .isGreaterThan(0);
            assertThat(v4.stream().anyMatch(a -> a.endsWith(":" + port))
                       || v6.stream().anyMatch(a -> a.endsWith(":" + port)))
                .as("the reader did not find THIS server's port %s in either table", port)
                .isTrue();
        } finally {
            server.stop(0);
        }
    }
}
