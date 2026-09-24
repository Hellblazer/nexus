// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.graalvm.nativeimage.hosted.Feature;
import org.graalvm.nativeimage.hosted.RuntimeSystemProperties;

/**
 * Bake {@code java.net.preferIPv4Stack=true} as a runtime default in the
 * native image (RDR-218 Gap 2, nexus-ijue9.7).
 *
 * <p><strong>Why a Feature and not code in {@code main}.</strong> Binding
 * {@code 127.0.0.1} on Linux does not give an IPv4 socket: the JDK opens a
 * dual-stack AF_INET6 socket on {@code ::ffff:127.0.0.1}, which WSL2's
 * localhost relay does not forward, so a Windows host cannot reach a service
 * that is demonstrably listening. Everything else that looks like it should
 * fix that does not. Measured on GraalVM 25 (community, linux/amd64), socket
 * family read from {@code /proc/net/tcp*}:
 *
 * <pre>
 *   System.setProperty in main()         -> ::ffff:127.0.0.1   INERT
 *   native-image -D...=true (build CLI)  -> ::ffff:127.0.0.1   INERT
 *   runtime -D on the binary             -> 127.0.0.1          works
 *   this Feature                         -> 127.0.0.1          works
 * </pre>
 *
 * <p>The build CLI's {@code -D} is documented "for image build time only", and
 * a {@code setProperty} in {@code main} runs after the networking stack has
 * already read the value. Only a value present before the image starts has
 * any effect, and this is the supported way to put one there.
 *
 * <p><strong>It is a DEFAULT, not a lock.</strong> A runtime
 * {@code -Djava.net.preferIPv4Stack=false} overrides it — measured, both
 * directions. That is what makes the deployment gate possible:
 * {@code storage_service_daemon} passes {@code =false} when
 * {@code NX_SERVICE_IPV4_ONLY} is explicitly disabled, so a deployment that
 * turns out to need a dual-stack listener can have one.
 *
 * <p>Not "so Voyage and EgressProxy can reach IPv6", which is what this said
 * until the citation was checked: {@code EgressProxy.java:34} records that
 * the cloud egress proxy is IPv4. No deployment known to this repository
 * needs the opt-out, which is why IPv4-only is the default rather than the
 * thing you opt into.
 *
 * <p><strong>Phase matters.</strong> Registering in {@code afterRegistration}
 * fails the build with {@code ImageSingletons do not contain key
 * ...RuntimeSystemPropertiesSupport} — the support singleton is not present
 * that early. {@code beforeAnalysis} works. Do not "simplify" this to an
 * earlier hook.
 */
public final class Ipv4StackFeature implements Feature {

    /** The JVM property that selects the socket family. */
    public static final String PREFER_IPV4_PROPERTY = "java.net.preferIPv4Stack";

    @Override
    public void beforeAnalysis(BeforeAnalysisAccess access) {
        RuntimeSystemProperties.register(PREFER_IPV4_PROPERTY, "true");
    }
}
