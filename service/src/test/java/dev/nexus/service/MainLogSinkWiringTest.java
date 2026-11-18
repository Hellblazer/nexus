// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;

import java.nio.file.Files;
import java.nio.file.Path;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-dngtz.1.11: {@code Main.main} cannot run in a test, so the order of its first steps is pinned on its text, as
 * {@link MainTrustAnchorsWiringTest} does. The uncaught-exception handler and the java.util.logging formatter go in before
 * the first boot step, the singleton lock, the secret masking and the migration, so nothing that fails early prints a raw
 * line past logback. {@code UncaughtExceptionLogTest} and {@code LiquibaseLineSafeTest} cover what they do.
 */
class MainLogSinkWiringTest {

    @Test
    void theUncaughtHandlerAndTheJulFormatterAreInstalledBeforeTheFirstBootStepAndAfterTheTrustAnchorsMode() throws Exception {
        String src = Files.readString(Path.of("src/main/java/dev/nexus/service/Main.java"));
        int trust = src.indexOf("if (TrustAnchors.requested(args)) {");
        int handler = src.indexOf("dev.nexus.service.log.UncaughtExceptionLog.install();");
        int jul = src.indexOf("dev.nexus.service.log.LineSafeFormatter.install()");
        int goOffline = src.indexOf("        goOfflineAtStartup();");
        int masking = src.indexOf("dev.nexus.service.log.LogMasking.install()");
        int lock = src.indexOf("bootSingletonLock(ds");

        assertThat(trust).isPositive();
        assertThat(src.indexOf("dev.nexus.service.log.UncaughtExceptionLog.install();", handler + 1)).as("installed once").isNegative();
        assertThat(handler).as("after the trust-anchors mode, which starts nothing").isGreaterThan(trust);
        assertThat(jul).isGreaterThan(trust);
        assertThat(goOffline).as("before the first boot step").isGreaterThan(handler).isGreaterThan(jul);
        assertThat(masking).as("before the secret masking").isGreaterThan(handler).isGreaterThan(jul);
        assertThat(lock).as("non-vacuity: the singleton lock is in Main").isPositive();
        assertThat(lock).as("before the singleton lock").isGreaterThan(handler);
    }
}
